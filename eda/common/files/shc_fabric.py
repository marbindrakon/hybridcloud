#!/usr/bin/env python3
"""Fabric checks for the HybridFabric, CloudGateway and NetworkPlacement jobs (shc, design/P7-PLAN.md P7.3).

Reads one JSON request on stdin, prints one JSON result: {"errors": [...]} plus action-specific fields. Exit 0
unless the request itself is malformed. Pure computation: no API calls, nothing secret in or out.

actions:
  check-fabric    {spec, realization}                      core prefixes and the VPN pool (realization), node names;
                  infrastructure.hub and the service VRF (service_check); the root index pool (poolSize.egressPool)
                  with the service VRF on;
                  the core's IPv6 transport when set: loopbackPool6 an IPv6 network of at least /120, icmpSource6 one of
                  at least /96, the two apart, sidOffset6 above the highest provider-edge index and offset + index
                  inside the SRGB; the core's transport, one for the fabric: core.transport mpls (also when empty) or
                  srv6, srv6 needs core.loopbackPool6 and core.srv6.block; a block (when set) a /32 inside 5f00::/16
                  apart from the core's IPv6 ranges, attachments.linkPool6 and egress.ipv6.routedPrefixes, at most 255
                  nodes and a highest provider-edge index of at most 255. transport: the fabric's transport ("" when
                  it is neither)
  check-gateway   {fabric: spec, core: realization core, attachments: realization attachments, gateway: spec,
                  others: [CloudGateway objects or {name, spec}]}
                  nodes match the fabric's, one gateway per site, VLAN and site subnet unique on the fabric and the
                  VLAN outside the attachment circuits' pool,
                  site subnet clear of the core prefixes; transport wireguard: the peers' addresses inside the
                  site subnet and clear of the nodes' tunnel addresses
  check-network   {prefixes, reserved: [cidr], exclusive: [cidr], ranges?: [reserved-ranges rows], site?,
                  circuits: [{name, accept}]}   a HybridNetwork's
                  address space: no prefix wholly inside a platform range (reserved; the IPAM steps around the ones
                  inside a prefix), none overlapping an exclusive range in either direction (address space the
                  platform keeps for itself, e.g. its own globally routed space), none holding an accept entry of the
                  network's attachment circuits; ranges: the published rows, named with their purpose (a Refused one
                  overlapped in either direction, a SteppedAround one holding a prefix; site rows only for site);
                  reserved (also the exclusive ones and the rows) / circuits: the errors of each kind; overlaps:
                  [{prefix, range, purpose, rule}] every overlap with a Refused row; reason PrefixReserved or ""
  pe-addresses    {core, index, nodeOrdinal?}   a provider edge's loopback (/32), core link address, IS-IS NET and
                  node SID label (index 1 is the border gateway root; the same addresses on every node); with
                  core.loopbackPool6 also its IPv6 loopback (loopback6 /128, loopbackIp6) and the IPv6 loopback's SID
                  label (sid6 = SRGB start + sidOffset6 + index), else "" / 0; with core.icmpSource6 and nodeOrdinal
                  (the node's 1-based position in borderGateway.nodes) icmp6, the namespace's ICMPv6 source on that
                  node (network address + (nodeOrdinal << 16) + index, /128), else ""; with core.srv6.block and
                  nodeOrdinal the SRv6 locator (the /48 of the block with node field locatorNode = nodeOrdinal * 256 +
                  index, both 1-255; unique across nodes), else "" / 0
  cloudvirt-namespaces {entity, cloudvirt, namespaces: [{name, network?}], network?}
                  the site namespace of each CloudVirt namespace: <entity>-<cloudvirt>-<name>, cut to 54 characters
                  plus a hash when longer than 63 (namespace names are global on the site's cluster), with its
                  labels; selector = all of the environment's namespaces, networkSelector = those naming network
  placement-names {namespace, name, entity, network, l3Vni}  names and labels of a placement's realized objects
  orphan-report   {crs, ledgers, sites, nodes}  the fabric orphan check's findings (see orphan_report)
  egress-plan     {egress: realization egress, nodes: [node names], networks: [{network, vpnTarget, index,
                  internet?, ipv4?, ipv6?: {block, prefixes, edgeSources6?}}], domainAsn, core?, translation?,
                  service? (the service VRF is on: the egress links use the link pool's lower half)}
                  per node: the hand-off (egress VRF on the node's site network) and per network a VRF e<vpnTarget>
                  with a veth pair xe/xo into the egress VRF; internet (default true) with ipv4 (default true): a
                  default route and one source NAT address per node (the node's natPool + the network's egress
                  index). With egress.ipv6.enabled (needs core.loopbackPool6):
                  per node the hand-off's IPv6 side (address, the upstream sessions, the filter's lists; the
                  translator-embedded deny list from translation.<site>.pref64 and egress.denyDestinations; the
                  resolvers on port 53, egress.ipv6.resolverDestinations6 and every translation.<site>.dns64Resolvers)
                  and per network with ipv6 (its platform block and the placement prefixes inside it, all inside
                  egress.ipv6.routedPrefixes) the IPv6 session over the veth, the announced prefixes, whether the
                  node is the network's standby (index mod nodes) and the sources the node's filter admits from the
                  network (src6: the block and, with core.icmpSource6, the node's /112 of it, which holds the ICMPv6
                  sources of all of that node's namespaces; the node's position is its place in nodes, 1-based; and
                  edgeSources6, the ICMPv6 sources of the cloud edge routers the network's placements use, as /128s,
                  each only when it lies in core.icmpSource6); errors for an unusable configuration
  circuit-plan    {attachments: realization attachments, nodes: [node names], index, ipv6?}  an attachment circuit's
                  link subnet (the index-th of attachments.linkPool), each node's address and the CE's; with ipv6 (the
                  circuit has IPv6 accept entries) also its IPv6 link under ipv6 (the index-th /64 of
                  attachments.linkPool6, the same host numbers), an error with reason FamilyUnavailable when there is
                  none
  check-circuit   {accept: [prefix-list entries], space: [the network's prefixes], reserved: [cidr], egressInternet,
                  advertiseDefault, attachments?, core?, translation?, egressInternet6?, ranges?: [reserved-ranges
                  rows], hub6?: [the IPv6 infrastructure.hub ranges]}  entries of either family
                  inside the network's address space or a platform range (reserved, ranges; IPv6: platform_ranges6)
                  are refused (errors; reason PrefixReserved for a published range, named with its purpose); IPv6
                  entries on a fabric without attachments.linkPool6 or on a network without
                  IPv6 address space are refused (familyErrors); notes for policy that doesn't apply now;
                  acceptsDefault (0.0.0.0/0), acceptsDefault6 (::/0), ipv6 (the list has IPv6 entries).
                  ceKind platform: see check_platform_circuit ({namespace, fabricNamespace, accept, advertise?,
                  family?, realization} -> errors, reason NotPlatformNamespace | ServiceNotOffered | InvalidSpec |
                  FamilyUnavailable, ceConfig {advertisedRanges, acceptedRanges})
  (check-fabric also validates the realization's attachments: poolSize.attachVlanPool, poolSize.attachLinks, and
  the optional linkPool6 and reserved6)
  attachments-plan {fabric, attachments: realization attachments, nodes: [node names], tableBase, domainAsn,
                  circuits, networks, placements: [CR objects], ledger: alloc, egress: egress-plan networks,
                  egressNodes: egress-plan nodes, reserved: [cidr], platformSources: [the IPv4 infrastructure.hub
                  ranges: a customer circuit's traffic never reaches them], hub6?: [the IPv6 ones], passwords: [circuits
                  (namespace/name) whose BGP password the job holds], core?, translation?, service?: realization
                  service, fabricNamespace?}  the circuits the fabric's
                  nodes realize: per circuit its VRF (the
                  network's root VRF e<vpnTarget>), VLAN device, conntrack zone (the network's egress zone, else its
                  VPN target) and routing policy (accept list; deny = the network's address space (spec.prefixes and
                  status.addressSpace) and its placements' prefixes (status.prefixes), the reserved ranges and
                  0.0.0.0/0 while the network wants internet egress; advertise = the placement prefixes and 0.0.0.0/0
                  when asked for and the network has internet egress). A circuit with IPv6 accept entries also gets
                  ipv6 {link, prefixLength, ce, nodes, accept, deny, advertise, denyDefault, advertiseDefault,
                  sources}: deny = the network's IPv6 space and placement prefixes, platform_ranges6 and ::/0
                  (always: the site gateways keep their own IPv6 default); advertise = the IPv6 placement prefixes
                  and ::/0 when asked for and the network has IPv6 internet egress; sources = the accepted prefixes and the link (what the
                  nodes admit as source from the CE). A circuit without IPv6 entries plans exactly as before. Per
                  node the root's attachments (root.json); circuits it can't realize under skipped; vrfs: per node
                  the networks with a root VRF (egress entry and/or circuits), for the node's FRR configuration.
                  Circuits of ceKind platform (in the fabric's namespace, with the service VRF on) are planned under
                  platform (VRF = the service VRF, no network, nothing in the circuits' filter table): their link,
                  device, session settings and nodes; service-plan gives them their routing policy
  reserved-ranges {realization, sites?: [{name, kind, ranges}]}  ranges: every range a tenant prefix is refused in or
                  stepped around (reserved_ranges): [{prefix, purpose, rule (Refused|SteppedAround), site?}]
  service-plan    {realization, nodes: [node names], fabric, domainAsn, networks: [HybridNetwork objects], ledger:
                  alloc, circuits: attachments-plan platform, vrfs: attachments-plan vrfs, previous: the fabric's
                  status.realization.service}   the service VRF (realization service, infrastructure.hub): a network
                  takes part with the rows of status.platformAccess.prefixes it accepted (state ready, or pending
                  with no reason), the VPN target
                  and root index (ledger kind egress) it holds; a prefix is accepted when it lies inside the network's
                  IPv4 space, clear of the reserved ranges, inside service.attachableRanges (when set), is recorded in
                  the ledger (kind servicePrefix, owner HybridNetwork/<ns>/<name>/<prefix>; not yet: pending) and
                  overlaps no prefix accepted for another network: first come first served (a prefix realized before
                  keeps its place, then the ledger's value, the lower first); else refused (ServicePrefixInUse,
                  PrefixReserved, OutsideAttachableRanges, OutsideAddressSpace, FamilyUnavailable). Per node with a
                  platform circuit the root's service (root.json): the VRF, the hub's IPv4 ranges, ports, hub
                  endpoints, the circuits with the node's address, the IN policy (accept the hub ranges le 32; deny
                  0.0.0.0/0, the link and core pools, the accepted prefixes), the accepted prefixes announced to the
                  hub, the attachable ranges, and per network with accepted prefixes its service link (the root index's
                  /31 in the upper half of egress.linkPool: so<t> in the service VRF the even address, se<t> in e<t>
                  the odd one); vrfs: the root VRFs with each such network's link under service (a network without
                  egress or circuits gets an entry of its own); status: HybridFabric status.realization.service
                  {enabled, vrf, sources, ports, hubEndpoints, attachableRanges, circuits: [{circuit, vlan, nodes, rev,
                  policy, device}], networks: [{network, vpnTarget, index, prefixes}], refused: [{network, prefix,
                  reason, message}]}; pending: what waits (not in status)
  service-prefix-check {prefixes: [{prefix, cluster?}], space, realization | ranges + attachableRanges, accepted}
                  one network's prefixes judged on their own (rows {prefix, cluster, state pending|refused, reason,
                  message}), for the network job
  check-cluster-networks {clusterNetwork, serviceNetwork, machineNetwork, provider, network, space, ranges |
                  realization + sites, site}  a cluster's own networks against each other, the network's space and the
                  reserved ranges (see check_cluster_networks)
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
    errors += core6_check(core, srgb, pe_max, len((spec.get("borderGateway") or {}).get("nodes") or []))
    errors += icmp4_check(core)
    node_names = [n.get("name") for n in (spec.get("borderGateway") or {}).get("nodes") or []]
    errors += srv6_check(core, real, node_names, pe_max)
    service_on = bool((real.get("service") or {}).get("enabled")) if isinstance(real.get("service"), dict) else False
    eg = egress_check(real.get("egress") or {}, [n.get("name") for n in (spec.get("borderGateway") or {}).get("nodes") or []],
                      spec.get("domainAsn"), core, service_on)
    errors += eg["errors"]
    size["egressPool"] = eg["poolSize"]
    errors += service_check(real)
    errors += router_advertisement_check(real)
    at = attachments_check(real.get("attachments") or {}, [n.get("name") for n in (spec.get("borderGateway") or {}).get("nodes") or []], core)
    errors += at["errors"]
    size["attachVlanPool"], size["attachLinks"] = at["vlanPool"], at["links"]
    t = core_transport(core)
    return {"errors": errors, "poolSize": size, "peIndexMax": pe_max, "transport": t if t in TRANSPORTS else ""}


TRANSPORTS = ("mpls", "srv6")
SRV6_SPACE = ipaddress.ip_network("5f00::/16")
# micro-SID layout F3216: block 32 bits, node 16 bits (node position 8 + provider-edge index 8), function 16 bits
SRV6_ORDINAL_MAX = 255


def core_transport(core):
    """The fabric's VPN transport as configured: core.transport, mpls when unset or empty. Not validated: srv6_check
    names a value outside TRANSPORTS."""
    v = (core or {}).get("transport")
    return "mpls" if v is None or str(v) == "" else str(v)


def srv6_block(core):
    """The SID block (core.srv6.block) as an IPv6 network, None when empty; ValueError when it is not a /32 inside
    5f00::/16."""
    s = (core or {}).get("srv6")
    if s is None or s == "":
        return None
    if not isinstance(s, dict):
        raise ValueError("realization core.srv6 is not a mapping")
    v = s.get("block") or ""
    if not str(v).strip():
        return None
    try:
        n = ipaddress.ip_network(str(v), strict=True)
    except ValueError as e:
        raise ValueError(f"realization core.srv6.block: {e}")
    if n.version != 6 or n.prefixlen != 32 or not n.subnet_of(SRV6_SPACE):
        raise ValueError(f"realization core.srv6.block {n} is not a /32 inside {SRV6_SPACE}")
    return n


def srv6_check(core, real, node_names, pe_max):
    """The core's transport, one for the fabric: core.transport mpls or srv6; srv6 needs core.loopbackPool6 and
    core.srv6.block (there is no per-node transport: a core.transportByNode is refused). A block, when set (also on
    mpls: the provider edges' locators are derived from it), is a /32 inside 5f00::/16 apart from the core's other
    IPv6 ranges and the routed IPv6 egress space, and the node positions and provider-edge indexes fit its 8-bit
    fields."""
    errors = []
    t = core.get("transport")
    if t is not None and str(t) != "" and str(t) not in TRANSPORTS:
        errors.append(f"realization core.transport {t!r} is not one of {', '.join(TRANSPORTS)}")
    if core.get("transportByNode"):
        errors.append("realization core.transportByNode is not supported: the fabric has one transport (core.transport)")
    block, block_bad = None, False
    try:
        block = srv6_block(core)
    except ValueError as e:
        errors.append(str(e))
        block_bad = True
    if core_transport(core) == "srv6":
        if not str(core.get("loopbackPool6") or "").strip():
            errors.append("realization core.transport srv6 needs the dual-stack core (core.loopbackPool6)")
        if block is None and not block_bad:
            errors.append("realization core.transport srv6 needs core.srv6.block")
    if block is None:
        return errors
    others = []
    for k in ("loopbackPool6", "icmpSource6"):
        try:
            c = core6(core, k, 128)
        except ValueError:
            continue  # core6_check names it
        if c is not None:
            others.append((f"core.{k}", c))
    e6 = ((real or {}).get("egress") or {}).get("ipv6") or {}
    for c in e6.get("routedPrefixes") or []:
        try:
            n = ipaddress.ip_network(str(c), strict=True)
        except ValueError:
            continue  # egress6_check names it
        others.append(("egress.ipv6.routedPrefixes", n))
    for what, c in others:
        if c.version == 6 and c.overlaps(block):
            errors.append(f"realization core.srv6.block {block} overlaps {what} {c}")
    if len(node_names) > SRV6_ORDINAL_MAX:
        errors.append(f"realization core.srv6.block: {len(node_names)} border gateway nodes do not fit the locator's "
                      f"node position (at most {SRV6_ORDINAL_MAX})")
    if pe_max > SRV6_ORDINAL_MAX:
        errors.append(f"realization core.srv6.block: the highest provider-edge index {pe_max} does not fit the "
                      f"locator's index (at most {SRV6_ORDINAL_MAX}: a core.loopbackPool or core.linkSubnet of at most "
                      "256 addresses)")
    return errors


def core6(core, key, min_len):
    """The IPv6 network of core.<key>, None when empty; ValueError when it isn't an IPv6 network of at least /min_len."""
    v = core.get(key) or ""
    if not str(v).strip():
        return None
    n = ipaddress.ip_network(str(v), strict=True)
    if n.version != 6:
        raise ValueError(f"realization core.{key} {n} is not an IPv6 network")
    if n.prefixlen > min_len:
        raise ValueError(f"realization core.{key} {n} is too small (at least a /{min_len})")
    return n


def icmp4_check(core):
    """core.icmpSource4: empty, or one IPv4 address (a /32 written out is the same) that is neither link-local nor
    inside the core's own IPv4 ranges (it is local in every root VRF)"""
    v = str(core.get("icmpSource4") or "").strip()
    if not v:
        return []
    try:
        a = ipaddress.ip_interface(v)
    except ValueError:
        return [f"realization core.icmpSource4 {v!r} is no IPv4 address"]
    if a.version != 4 or a.network.prefixlen != 32:
        return [f"realization core.icmpSource4 {v} is no single IPv4 address"]
    if a.ip.is_link_local or a.ip.is_loopback or a.ip.is_multicast or a.ip.is_unspecified:
        return [f"realization core.icmpSource4 {v} is link-local, loopback, multicast or unspecified"]
    for key in ("loopbackPool", "linkSubnet"):
        try:
            if a.ip in ipaddress.ip_network(str(core.get(key) or "0.0.0.0/32"), strict=False):
                return [f"realization core.icmpSource4 {v} lies inside core.{key}"]
        except ValueError:
            pass
    return []


def core6_check(core, srgb, pe_max, node_count):
    """The core's IPv6 transport (loopbackPool6, sidOffset6, icmpSource6): sizes, the SR indexes inside the SRGB and
    clear of the IPv4 ones, the two ranges apart."""
    errors, lo6, icmp6 = [], None, None
    try:
        lo6 = core6(core, "loopbackPool6", 120)
    except ValueError as e:
        errors.append(str(e))
    try:
        icmp6 = core6(core, "icmpSource6", 96)
    except ValueError as e:
        errors.append(str(e))
    if lo6 is not None:
        try:
            off = int(core.get("sidOffset6", 1000))
        except (TypeError, ValueError):
            off = -1
        span = srgb["end"] - srgb["start"]
        if off < 1:
            errors.append(f"realization core.sidOffset6 {core.get('sidOffset6')!r} is not a positive number")
        elif off + pe_max > span:
            errors.append(f"realization core.sidOffset6 {off} + the highest provider-edge index {pe_max} is outside "
                          f"core.srgb {srgb['start']}-{srgb['end']}")
        elif off <= pe_max:
            # an IPv6 loopback's index would equal another provider edge's IPv4 index
            errors.append(f"realization core.sidOffset6 {off} is not above the highest provider-edge index {pe_max}")
        if lo6.num_addresses <= pe_max:
            errors.append(f"realization core.loopbackPool6 {lo6} is smaller than the provider-edge indexes (1-{pe_max})")
    if icmp6 is not None and (node_count << 16) + pe_max >= icmp6.num_addresses:
        errors.append(f"realization core.icmpSource6 {icmp6} has no room for {node_count} node(s)")
    if lo6 is not None and icmp6 is not None and lo6.overlaps(icmp6):
        errors.append(f"realization core.loopbackPool6 {lo6} overlaps core.icmpSource6 {icmp6}")
    return errors


EGRESS_RESERVED_TABLES = {253, 254, 255}


def egress_check(eg, node_names, domain_asn=0, core=None, service=False):
    """Validate the realization's egress settings for the fabric's nodes. poolSize = the root indexes (a network with
    internet egress or, with the service VRF on, with service prefixes holds one): the smallest node NAT pool, and no
    more than the link pool's /31s; with the service VRF on (service) the link pool is split in halves, egress links
    in the lower, service links in the upper, so no more than the /31s of one half. Egress off: 0, or with the service
    VRF on the /31s of the link pool's upper half (the service links alone need the index)."""
    if not eg.get("enabled"):
        if not service:
            return {"errors": [], "poolSize": 0}
        try:
            link = ipaddress.ip_network(str(eg.get("linkPool") or "169.254.0.0/16"), strict=True)
        except ValueError as e:
            return {"errors": [f"realization egress.linkPool: {e}"], "poolSize": 0}
        if link.version != 4 or link.prefixlen > 29:
            return {"errors": [f"realization egress.linkPool {link} must be an IPv4 network of at least a /29 for the "
                               "service links"], "poolSize": 0}
        return {"errors": [], "poolSize": link.num_addresses // 4}
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
        sizes.append(link.num_addresses // (4 if service else 2))
        if service and (link.version != 4 or link.prefixlen > 29):
            errors.append(f"realization egress.linkPool {link} must be an IPv4 network of at least a /29 for the "
                          "service links")
    except ValueError as e:
        errors.append(f"realization egress.linkPool: {e}")
    vrf = eg.get("vrf") or {}
    if int(vrf.get("table") or 0) in EGRESS_RESERVED_TABLES or not vrf.get("name"):
        errors.append("realization egress.vrf needs a name and a table other than 253-255")
    errors += egress6_check(eg.get("ipv6") or {}, node_names, domain_asn, core)
    return {"errors": errors, "poolSize": min(sizes) if sizes and not errors else 0}


def _nets6(values, what, errors):
    out = []
    for c in values or []:
        try:
            n = ipaddress.ip_network(str(c), strict=True)
        except ValueError as e:
            errors.append(f"realization egress.ipv6.{what}: {e}")
            continue
        if n.version != 6:
            errors.append(f"realization egress.ipv6.{what}: {n} is not IPv6")
            continue
        out.append(n)
    return out


def egress6_check(e6, node_names, domain_asn=0, core=None):
    """Validate the realization's IPv6 egress for the fabric's nodes (nothing when it is off): the routed
    prefixes, the AS (neither the domain's nor an upstream's), the upstream routers and each node's address on one
    hand-off network, and the dual-stack core it needs."""
    if not e6.get("enabled"):
        return []
    errors = []
    if not str((core or {}).get("loopbackPool6") or ""):
        errors.append("realization egress.ipv6 needs the dual-stack core (core.loopbackPool6)")
    routed = _nets6(e6.get("routedPrefixes"), "routedPrefixes", errors)
    if not routed:
        errors.append("realization egress.ipv6.routedPrefixes is required when IPv6 egress is enabled")
    for r in routed:
        if r.prefixlen > 63:
            errors.append(f"realization egress.ipv6.routedPrefixes: {r} leaves no room for placement prefixes up to /64")
    try:
        asn = int(e6.get("asn") or 0)
    except (TypeError, ValueError):
        asn = 0
    if not 0 < asn < 4294967296:
        errors.append("realization egress.ipv6.asn is required when IPv6 egress is enabled")
    elif asn == int(domain_asn or 0):
        errors.append(f"realization egress.ipv6.asn {asn} is the fabric's own domainAsn")
    ups = e6.get("upstream") or []
    if not ups:
        errors.append("realization egress.ipv6.upstream is required when IPv6 egress is enabled")
    up_addrs = []
    for u in ups:
        try:
            a = ipaddress.ip_address(str((u or {}).get("address")))
            ua = int((u or {}).get("asn") or 0)
        except (TypeError, ValueError):
            errors.append(f"realization egress.ipv6.upstream: {u} needs an address and an asn")
            continue
        if a.version != 6 or ua <= 0:
            errors.append(f"realization egress.ipv6.upstream: {u} needs an IPv6 address and an asn")
            continue
        if ua == asn:
            errors.append(f"realization egress.ipv6.upstream {a}: its asn {ua} is the egress VRF's own")
        up_addrs.append(a)
    nets = set()
    for name in node_names:
        n = (e6.get("nodes") or {}).get(name) or {}
        try:
            addr = ipaddress.ip_interface(str(n.get("address")))
        except ValueError as e:
            errors.append(f"realization egress.ipv6.nodes.{name}: address/length required ({e})")
            continue
        if addr.version != 6 or addr.network.prefixlen == 128:
            errors.append(f"realization egress.ipv6.nodes.{name}.address {addr} is not an IPv6 address with its network length")
            continue
        nets.add(addr.network)
        for a in up_addrs:
            if a not in addr.network:
                errors.append(f"realization egress.ipv6.nodes.{name}.address {addr} is not on the upstream {a}'s network")
        for r in routed:
            if addr.ip in r:
                errors.append(f"realization egress.ipv6.nodes.{name}.address {addr} lies inside the routed prefix {r}")
    if len(nets) > 1:
        errors.append("realization egress.ipv6.nodes: the nodes' addresses are not on one hand-off network")
    try:
        mtu = int(e6.get("wanMtu") or 1420)
        if not 1280 <= mtu <= 65535:
            errors.append(f"realization egress.ipv6.wanMtu {mtu} is outside 1280-65535")
    except (TypeError, ValueError):
        errors.append("realization egress.ipv6.wanMtu is not a number")
    _nets6(e6.get("denyDestinations6"), "denyDestinations6", errors)
    _nets6(e6.get("allowDestinations6"), "allowDestinations6", errors)
    for c in e6.get("resolverDestinations6") or []:
        try:
            if ipaddress.ip_address(str(c)).version != 6:
                raise ValueError("not IPv6")
        except ValueError:
            errors.append(f"realization egress.ipv6.resolverDestinations6: {c} is not an IPv6 address")
    for k, v in (e6.get("externalSitePrefixes") or {}).items():
        for p in _nets6(v, f"externalSitePrefixes.{k}", errors):
            if not any(p.subnet_of(r) for r in routed):
                errors.append(f"realization egress.ipv6.externalSitePrefixes.{k}: {p} is outside the routed prefixes")
    return errors


def plat_deny6(translation, deny4):
    """The translator-embedded forms of the IPv4 deny list: for every translation prefix (a /96) each IPv4 range
    becomes prefix + range (length 96 + its length), so a translator never re-opens them."""
    out = []
    for site in sorted((translation or {}).keys()):
        p = str(((translation or {}).get(site) or {}).get("pref64") or "")
        if not p:
            continue
        try:
            pref = ipaddress.ip_network(p, strict=True)
        except ValueError:
            continue
        if pref.version != 6 or pref.prefixlen != 96:
            continue
        for d in deny4 or []:
            try:
                n4 = ipaddress.ip_network(str(d), strict=True)
            except ValueError:
                continue
            if n4.version != 4:
                continue
            emb = ipaddress.ip_network((int(pref.network_address) + int(n4.network_address), 96 + n4.prefixlen))
            if emb not in out:
                out.append(emb)
    return [str(n) for n in sorted(out, key=lambda n: (int(n.network_address), n.prefixlen))]


def resolvers6(e6, translation):
    """The resolver addresses the IPv6 filter admits on port 53: egress.ipv6.resolverDestinations6 and the DNS64
    resolvers of every site's translation entry (translation.<site>.dns64Resolvers), so a site that hands its
    IPv6-only placements DNS64 resolvers needs no second list. Single addresses (the filter's set has no intervals; a
    /128 is written as its address), sorted and without duplicates; anything that is not one IPv6 address is left out
    (the realization check names a bad resolverDestinations6 entry)."""
    out = set()
    given = list(e6.get("resolverDestinations6") or [])
    for site in sorted((translation or {}).keys()):
        given += list(((translation or {}).get(site) or {}).get("dns64Resolvers") or [])
    for c in given:
        try:
            n = ipaddress.ip_network(str(c), strict=True)
        except ValueError:
            continue
        if n.version == 6 and n.prefixlen == 128:
            out.add(n.network_address)
    return [str(a) for a in sorted(out)]


def egress6_plan(eg, req, node_names, errors):
    """The IPv6 side of egress-plan: per node its hand-off and filter lists, per network (the ones with ipv6) the
    prefixes it announces and the node that is its standby."""
    e6 = eg.get("ipv6") or {}
    routed = [ipaddress.ip_network(str(c), strict=True) for c in e6.get("routedPrefixes") or []]
    networks = {}
    for w in req.get("networks") or []:
        v6 = w.get("ipv6")
        if not v6:
            continue
        try:
            block = ipaddress.ip_network(str(v6.get("block")), strict=True)
        except ValueError as e:
            errors.append(f"{w['network']}: IPv6 block: {e}")
            continue
        if block.version != 6 or not any(block.subnet_of(r) for r in routed):
            errors.append(f"{w['network']}: IPv6 block {block} is outside the routed prefixes")
            continue
        prefixes = []
        for c in v6.get("prefixes") or []:
            try:
                p = ipaddress.ip_network(str(c), strict=True)
            except ValueError as e:
                errors.append(f"{w['network']}: IPv6 prefix {c}: {e}")
                continue
            if p.version != 6 or not p.subnet_of(block):
                errors.append(f"{w['network']}: IPv6 prefix {p} is outside the network's block {block}")
                continue
            if p.prefixlen > 64:
                errors.append(f"{w['network']}: IPv6 prefix {p} is longer than /64")
                continue
            if str(p) not in prefixes:
                prefixes.append(str(p))
        networks[w["network"]] = {"block": str(block), "prefixes": sorted(prefixes, key=lambda s: (ipaddress.ip_network(s).network_address, ipaddress.ip_network(s).prefixlen)),
                                  "index": int(w["index"])}
    count = max(1, len(node_names))
    for k, v in networks.items():
        v["primary"] = node_names[v["index"] % count] if node_names else ""
    nodes = {}
    for name in node_names:
        n = (e6.get("nodes") or {}).get(name) or {}
        addr = ipaddress.ip_interface(str(n.get("address")))
        nodes[name] = {
            "address": str(addr), "ip": str(addr.ip), "asn": int(e6.get("asn") or 0),
            "upstream": [{"address": str(ipaddress.ip_address(str(u.get("address")))), "asn": int(u.get("asn"))}
                         for u in e6.get("upstream") or []],
            "wanMtu": int(e6.get("wanMtu") or 1420), "mss": int(e6.get("wanMtu") or 1420) - 60,
            "maxPrefixesUpstream": int(e6.get("maxPrefixesUpstream") or 10),
            "maxPrefixesNetwork": int(e6.get("maxPrefixesNetwork") or 1000),
            "routed": [str(r) for r in routed],
            "standbyCommunity": int(e6.get("standbyCommunity") or 998),
            "standbyPrepend": int(e6.get("standbyPrepend") or 2),
            "deny6": [str(c) for c in e6.get("denyDestinations6") or []],
            "allow6": [str(c) for c in e6.get("allowDestinations6") or []],
            "resolvers6": resolvers6(e6, req.get("translation")),
            "platDeny6": plat_deny6(req.get("translation"), eg.get("denyDestinations")),
            "external6": sorted({str(p) for v in (e6.get("externalSitePrefixes") or {}).values() for p in v or []}),
        }
    return nodes, networks


def icmp6_slice(core, ordinal):
    """The node's slice of core.icmpSource6: the /112 holding network + (ordinal << 16) + every provider-edge index,
    i.e. the ICMPv6 sources of that node's root and gateway namespaces (pe_addresses' icmp6) and of nothing else
    (another node's, or a cloud edge router's at its own ordinal, are in other slices). None without the pool."""
    pool = core6(core, "icmpSource6", 96)
    if pool is None:
        return None
    base = int(pool.network_address) + (int(ordinal) << 16)
    if ordinal < 1 or base + 0xFFFF > int(pool.broadcast_address):
        raise ValueError(f"node position {ordinal} does not fit core.icmpSource6 {pool}")
    return ipaddress.ip_network((base, 112))


def icmp6_slices(core, node_names, errors):
    """{node: its slice of core.icmpSource6 (text)} by the node's 1-based position in the request's nodes (the order of
    borderGateway.nodes, as for pe-addresses' nodeOrdinal); {} without the pool"""
    out = {}
    for o, name in enumerate(node_names, 1):
        try:
            s = icmp6_slice(core, o)
        except ValueError as e:
            errors.append(str(e))
            return {}
        if s is None:
            return {}
        out[name] = str(s)
    return out


def edge_sources6(v6, core):
    """A network's edgeSources6 (the cloud edge routers' ICMPv6 sources, from their gateways' status) as canonical
    addresses, each once and in order: only the ones inside core.icmpSource6 (anything else is left out, so a source
    set never admits more than the platform's own ICMPv6 sources); [] without the pool"""
    try:
        pool = core6(core or {}, "icmpSource6", 96)
    except ValueError:
        return []
    if pool is None:
        return []
    out = []
    for a in v6.get("edgeSources6") or []:
        try:
            ip = ipaddress.ip_interface(str(a))
        except ValueError:
            continue
        if ip.version != 6 or ip.network.prefixlen != 128 or ip.ip not in pool:
            continue
        if str(ip.ip) not in out:
            out.append(str(ip.ip))
    return sorted(out, key=ipaddress.ip_address)


def egress_plan(req):
    eg, node_names = req.get("egress") or {}, req.get("nodes") or []
    chk = egress_check(eg, node_names, req.get("domainAsn"), req.get("core"), bool(req.get("service")))
    if chk["errors"]:
        return {"errors": chk["errors"], "poolSize": 0, "nodes": {}, "networks": {}}
    if not eg.get("enabled"):
        return {"errors": [], "poolSize": 0, "nodes": {}, "networks": {}}
    asn = int(req.get("domainAsn") or 0)
    link = ipaddress.ip_network(str(eg.get("linkPool") or "169.254.0.0/16"), strict=True)
    base = int(eg.get("networkTableBase") or 100000)
    plen = int(eg.get("prefixLength") or 24)
    errors, nodes, networks = [], {}, {}
    v6_on = bool((eg.get("ipv6") or {}).get("enabled"))
    nodes6, nets6 = egress6_plan(eg, req, node_names, errors) if v6_on else ({}, {})
    icmp_slices = icmp6_slices(req.get("core") or {}, node_names, errors) if v6_on else {}
    edge6 = {w["network"]: edge_sources6(w["ipv6"], req.get("core")) for w in req.get("networks") or []
             if v6_on and w.get("ipv6")}
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
            # internet: the IPv4 default route and source NAT (only for a network with IPv4 address space)
            internet = bool(w.get("internet", True)) and bool(w.get("ipv4", True))
            nat = str(pool.network_address + i) if internet else ""
            v6 = nets6.get(w["network"])
            e = {"network": w["network"], "vpnTarget": t, "vrf": f"e{t}", "table": base + t, "zone": i + 1,
                 "xe": f"xe{t}", "xo": f"xo{t}", "xeAddr": f"{xe}/31", "xoAddr": f"{xo}/31",
                 "xeIp": str(xe), "xoIp": str(xo), "nat": nat, "internet": internet,
                 # RD type 0 (2-byte AS : 4-byte number) when the AS allows, else the loopback form at render
                 "rd": f"{asn}:{t}" if 0 < asn <= 65535 else ""}
            if v6:
                # the IPv6 session over the veth, the prefixes announced, and the sources the filter admits: the
                # network's block and the node's slice of core.icmpSource6 (the ICMPv6 sources of the node's own
                # namespaces, so a gateway's Packet Too Big or Time Exceeded toward the internet passes; the root's
                # own is in it, and the node's job also adds that one by itself)
                # and the ICMPv6 sources of the cloud edge routers the network's placements use: their Time Exceeded
                # and Unreachable for a flow from the internet leave the network toward it by this node too
                e["ipv6"] = {"block": v6["block"], "prefixes": v6["prefixes"], "standby": v6["primary"] != name,
                             "src6": [v6["block"]] + ([icmp_slices[name]] if name in icmp_slices else [])
                             + edge6.get(w["network"], [])}
            out.append(e)
            entry = networks.setdefault(w["network"], {"vpnTarget": t, "index": i, "sourceAddresses": [],
                                                       "internet": internet,
                                                       "ipv6": bool(v6),
                                                       "prefixes6": v6["prefixes"] if v6 else [],
                                                       "primary": v6["primary"] if v6 else ""})
            if nat:
                entry["sourceAddresses"].append(nat)
        nodes[name] = {"vrf": (eg.get("vrf") or {}).get("name", "egress"), "table": int((eg.get("vrf") or {}).get("table", 9000)),
                       "parent": eg["parentInterface"], "mtu": int(eg.get("mtu") or 9000),
                       "address": f"{n['address']}/{plen}", "gateway": str(eg["gateway"]),
                       "deny": [str(c) for c in eg.get("denyDestinations") or []],
                       "allow": [str(c) for c in eg.get("allowDestinations") or []],
                       "networks": out}
        if v6_on:
            nodes[name]["ipv6"] = nodes6[name]
    return {"errors": errors, "poolSize": chk["poolSize"], "nodes": nodes, "networks": networks, "ipv6": v6_on}


def pe_addresses(req):
    core, i = req.get("core") or {}, int(req["index"])
    lo = ipaddress.ip_network(str(core["loopbackPool"]), strict=True)
    link = ipaddress.ip_network(str(core["linkSubnet"]), strict=True)
    srgb = core.get("srgb") or {"start": 16000, "end": 23999}
    if not 1 <= i <= min(lo.num_addresses - 2, link.num_addresses - 2, srgb["end"] - srgb["start"]):
        return {"errors": [f"provider-edge index {i} does not fit the core prefixes or the SRGB"]}
    area = core.get("isisArea") or "49.0001"
    out = {"errors": [], "index": i, "loopback": f"{lo.network_address + i}/32", "loopbackIp": str(lo.network_address + i),
           "link": f"{link.network_address + i}/{link.prefixlen}", "linkIp": str(link.network_address + i),
           "net": f"{area}.0000.0000.{i:04x}.00", "sid": srgb["start"] + i,
           "loopback6": "", "loopbackIp6": "", "sid6": 0, "icmp6": "", "locator": "", "locatorNode": 0}
    try:
        lo6, icmp6 = core6(core, "loopbackPool6", 120), core6(core, "icmpSource6", 96)
        block = srv6_block(core)
    except ValueError as e:
        return {"errors": [str(e)]}
    if block is not None and req.get("nodeOrdinal") not in (None, ""):
        # the SRv6 locator: the /48 of the block whose node field (bits 32-47) is (node position << 8) + index
        o = int(req["nodeOrdinal"])
        if not 1 <= o <= SRV6_ORDINAL_MAX or not 1 <= i <= SRV6_ORDINAL_MAX:
            return {"errors": [f"node position {o} or provider-edge index {i} does not fit the SRv6 locator "
                               f"(1-{SRV6_ORDINAL_MAX} each)"]}
        node = (o << 8) + i
        out.update({"locator": str(ipaddress.ip_network((int(block.network_address) + (node << 80), 48))),
                    "locatorNode": node})
    if lo6 is not None:
        sid6 = srgb["start"] + int(core.get("sidOffset6", 1000)) + i
        if i >= lo6.num_addresses or sid6 > srgb["end"]:
            return {"errors": [f"provider-edge index {i} does not fit core.loopbackPool6 or the SRGB (sidOffset6)"]}
        # str() of an ip_address is the canonical compressed form the kernel and FRR print, so the apply and the
        # readiness checks can compare addresses as text
        a6 = lo6.network_address + i
        out.update({"loopback6": f"{a6}/128", "loopbackIp6": str(a6), "sid6": sid6})
    o = req.get("nodeOrdinal")
    if icmp6 is not None and o not in (None, ""):
        o = int(o)
        a = int(icmp6.network_address) + (o << 16) + i
        if o < 1 or a > int(icmp6.broadcast_address):
            return {"errors": [f"node position {o} does not fit core.icmpSource6 {icmp6}"]}
        out["icmp6"] = f"{ipaddress.IPv6Address(a)}/128"
    return out


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
    # a tunnel gateway's WireGuard socket is bound on the node itself (where the device was created), so two tunnel
    # gateways of a fabric on the same nodes need different listen ports (the default is the realization's)
    default_port = req.get("wireguardListenPort")

    def wg_port(spec):
        if (spec.get("transport") or "none") != "wireguard":
            return None
        p = (spec.get("wireguard") or {}).get("listenPort", default_port)
        return int(p) if p not in (None, "") else None

    port = wg_port(gw)
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
        if port is not None and wg_port(ospec) == port:
            errors.append(f"WireGuard listen port {port} is already used by CloudGateway {o.get('name')} on the same "
                          f"border gateway nodes; set spec.wireguard.listenPort to a free port")
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
    # address space the platform keeps for itself: no tenant prefix may overlap it, in either direction
    seen = []
    for r in req.get("exclusive") or []:
        try:
            rn = ipaddress.ip_network(str(r), strict=False)
        except ValueError:
            continue
        if rn in seen:
            continue
        seen.append(rn)
        for a in mine:
            if a.version == rn.version and a.overlaps(rn):
                reserved.append(f"prefix {a} overlaps address space the platform keeps for itself ({rn})")
    # the published ranges (reserved_ranges rows), named with their purpose: a Refused one overlapped in either
    # direction, a SteppedAround one holding a prefix; overlaps lists every overlap with a Refused range (a network
    # from before a range became exclusive keeps working, the network job tells)
    overlaps_out = []
    for n, purpose, rule, site in rows_of(req.get("ranges")):
        if site and site != str(req.get("site") or ""):
            continue
        for a in mine:
            if a.version != n.version:
                continue
            if rule == REFUSED and a.overlaps(n):
                overlaps_out.append({"prefix": str(a), "range": str(n), "purpose": purpose, "rule": rule})
                reserved.append(reserved_message("prefix", a, (n, purpose, rule)))
            elif rule != REFUSED and a.subnet_of(n):
                reserved.append(reserved_message("prefix", a, (n, purpose, rule)))
    # the circuits accept routes from their CE; the network's space never holds one (as check-circuit refuses)
    for c in req.get("circuits") or []:
        for e in c.get("accept") or []:
            entry = accept_entry(e)
            for a in mine:
                if entry and entry[0].version == a.version and entry[0].subnet_of(a):
                    circuits.append(f"prefix {a} holds accept entry '{e}' of AttachmentCircuit {c.get('name')}")
    out = {"errors": reserved + circuits, "reserved": reserved, "circuits": circuits}
    if req.get("ranges") is not None:
        out.update({"overlaps": overlaps_out, "reason": "PrefixReserved" if reserved else ""})
    return out


# ---------------------------------------------------------------------------------------------- reserved ranges
# The ranges a tenant prefix is refused in or stepped around (reserved_ranges): the realization key, the purpose shown
# to tenants and the rule. Refused: no tenant prefix, circuit route or cluster network may overlap them; SteppedAround:
# the address management steps around them (a prefix inside one has nothing to give out)
REFUSED, STEPPED = "Refused", "SteppedAround"
PURPOSE_HUB = "Networks the platform's management connects from"
RANGE_KEYS = (
    ("infrastructure.hub", PURPOSE_HUB, REFUSED),
    ("network.exclusiveRanges", "Address space the platform keeps for itself", REFUSED),
    ("ipam.publicPools", "The platform's assignable IPv6 space", REFUSED),
    ("core.srv6.block", "The platform's transport addresses", REFUSED),
    ("core.icmpSource6", "Addresses of the platform's network nodes", REFUSED),
    ("core.icmpSource4", "Addresses of the platform's network nodes", REFUSED),
    ("core.loopbackPool", "Addresses inside the platform's network nodes", STEPPED),
    ("core.linkSubnet", "Addresses inside the platform's network nodes", STEPPED),
    ("attachments.linkPool", "Addresses inside the platform's network nodes", STEPPED),
    ("egress.linkPool", "Addresses inside the platform's network nodes", STEPPED),
)
PURPOSE_SITE = "Addresses the site uses itself"
# a site's type (CloudInfrastructure spec.type) -> the backend kind of placement.siteReserved
SITE_KINDS = {"openshift": "CloudVirt", "openstack": "CloudOSO", "aws": "CloudAWS", "gcp": "CloudGCP"}


def _values(real, key):
    """The realization's values of one RANGE_KEYS key, as text (empty values left out)."""
    real = real or {}
    if key == "infrastructure.hub":
        vals = (real.get("infrastructure") or {}).get("hub") or []
    elif key == "network.exclusiveRanges":
        vals = (real.get("network") or {}).get("exclusiveRanges") or []
    elif key == "ipam.publicPools":
        pools = (real.get("ipam") or {}).get("publicPools") or {}
        vals = [c for name in sorted(pools) for c in ((pools[name] or {}).get("prefixes") or [])] \
            if isinstance(pools, dict) else []
    elif key == "core.srv6.block":
        s = (real.get("core") or {}).get("srv6")
        vals = [s.get("block")] if isinstance(s, dict) else []
    else:
        top, sub = key.split(".")
        vals = [((real.get(top) or {}).get(sub))]
    if isinstance(vals, (str, bytes)):
        vals = [vals]
    return [str(v).strip() for v in vals if v is not None and str(v).strip()]


def _net(text):
    """A network of either family (a bare address is its /32 or /128), None when unusable."""
    try:
        return ipaddress.ip_network(str(text).strip(), strict=True)
    except ValueError:
        return None


def range_rows(real, sites=None):
    """[(key, network, purpose, rule, site)] in the order of reserved_ranges; unusable values are left out (the
    realization check names them), duplicates once."""
    out, seen = [], set()
    for key, purpose, rule in RANGE_KEYS:
        for v in _values(real, key):
            n = _net(v)
            if n is not None and (n, rule, "") not in seen:
                seen.add((n, rule, ""))
                out.append((key, n, purpose, rule, ""))
    siteres = ((real or {}).get("placement") or {}).get("siteReserved") or {}
    for s in sites or []:
        name = str(s.get("name") or "")
        kind = SITE_KINDS.get(str(s.get("kind") or ""), str(s.get("kind") or ""))
        own = list(s.get("ranges") or []) + list(siteres.get(kind) or [] if isinstance(siteres, dict) else [])
        for v in own:
            n = _net(v)
            if n is not None and name and (n, STEPPED, name) not in seen:
                seen.add((n, STEPPED, name))
                out.append(("placement.siteReserved", n, PURPOSE_SITE, STEPPED, name))
    return out


def reserved_ranges(real, sites=None):
    """Every range a tenant prefix is refused in (rule Refused) or stepped around (rule SteppedAround), each with its
    purpose in words and, for a site's own ranges, the site: infrastructure.hub, network.exclusiveRanges, the
    ipam.publicPools prefixes, core.srv6.block, core.icmpSource6 and core.icmpSource4 (Refused); core.loopbackPool,
    core.linkSubnet, attachments.linkPool and egress.linkPool (SteppedAround); per site (sites: [{name, kind (the
    site's type or the backend kind), ranges (its underlay)}]) its underlay and placement.siteReserved of its kind
    (SteppedAround, site). The published list (no realization key names in it)."""
    rows = []
    for _, n, purpose, rule, site in range_rows(real, sites):
        r = {"prefix": str(n), "purpose": purpose, "rule": rule}
        if site:
            r["site"] = site
        rows.append(r)
    return rows


def reserved_ranges_action(req):
    return {"errors": [], "ranges": reserved_ranges(req.get("realization") or {}, req.get("sites") or [])}


def rows_of(ranges):
    """Published range rows ({prefix, purpose, rule, site?}) as [(network, purpose, rule, site)]; unusable rows left out."""
    out = []
    for r in ranges or []:
        n = _net((r or {}).get("prefix"))
        if n is not None:
            out.append((n, str(r.get("purpose") or ""), str(r.get("rule") or REFUSED), str(r.get("site") or "")))
    return out


def lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def range_clash(net, rows, site=None):
    """The first row the network clashes with, as (row network, purpose, rule), or None: a Refused range overlapped in
    either direction, a SteppedAround range the network lies inside. Site rows count only for that site (site None:
    none of them)."""
    for n, purpose, rule, s in rows:
        if s and s != site:
            continue
        if n.version != net.version:
            continue
        if (rule == REFUSED and n.overlaps(net)) or (rule != REFUSED and net.subnet_of(n)):
            return n, purpose, rule
    return None


def reserved_message(what, net, clash):
    n, purpose, _ = clash
    return f"{what} {net} overlaps {n}, reserved by the platform: {lower_first(purpose)}"


# ---------------------------------------------------------------------------------------------------- service VRF
SERVICE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,14}$")
SERVICE_PREFIX_REASONS = ("ServicePrefixInUse", "PrefixReserved", "OutsideAttachableRanges", "OutsideAddressSpace",
                          "FamilyUnavailable")


def _ports(v, what, errors):
    try:
        ports = [int(p) for p in (v or [])]
    except (TypeError, ValueError):
        errors.append(f"realization {what} must be a list of TCP ports")
        return []
    if not ports or not all(1 <= p <= 65535 for p in ports):
        errors.append(f"realization {what} must hold TCP ports (1-65535)")
        return []
    return sorted(set(ports))


def hub_nets(real, version=None):
    """infrastructure.hub as networks (unusable entries left out), of one family when version is given"""
    out = [n for n in (_net(v) for v in _values(real, "infrastructure.hub")) if n is not None]
    return [n for n in out if version is None or n.version == version]


def realization_pools(real):
    """The realization's own address pools, [(key, network)]: what no hub range may overlap."""
    real = real or {}
    core, at, eg = real.get("core") or {}, real.get("attachments") or {}, real.get("egress") or {}
    out = []
    for key, v in (("core.loopbackPool", core.get("loopbackPool")), ("core.linkSubnet", core.get("linkSubnet")),
                   ("core.loopbackPool6", core.get("loopbackPool6")), ("core.icmpSource6", core.get("icmpSource6")),
                   ("core.icmpSource4", core.get("icmpSource4")),
                   ("attachments.linkPool", at.get("linkPool") if at.get("enabled") else None),
                   ("attachments.linkPool6", at.get("linkPool6") if at.get("enabled") else None),
                   ("egress.linkPool", eg.get("linkPool") if (eg.get("enabled") or (real.get("service") or {}).get("enabled")) else None)):
        n = _net(v) if v is not None and str(v).strip() else None
        if n is not None:
            out.append((key, n))
    for v in _values(real, "core.srv6.block"):
        n = _net(v)
        if n is not None:
            out.append(("core.srv6.block", n))
    for v in _values(real, "ipam.publicPools"):
        n = _net(v)
        if n is not None:
            out.append(("ipam.publicPools", n))
    for name, node in sorted(((eg.get("nodes") or {}) if eg.get("enabled") else {}).items()):
        n = _net((node or {}).get("natPool"))
        if n is not None:
            out.append((f"egress.nodes.{name}.natPool", n))
    return out


def service_check(real):
    """The realization's infrastructure ranges and service VRF (check-fabric). Always: infrastructure.hub entries are
    networks of either family, no realization pool overlaps one, and the retired egress.platformSources and
    egress.platformPorts are not set. With service.enabled: attachments.enabled, an IPv4 hub range, a VRF name (a
    Linux device name, not the egress VRF's, not e<digits>), a table not 253-255, not the egress table and below
    egress.networkTableBase, the ports, every hubEndpoints prefix an IPv4 network inside a hub range with its ports,
    every attachableRanges entry an IPv4 network clear of the reserved ranges."""
    real = real or {}
    errors = []
    inf = real.get("infrastructure") or {}
    if not isinstance(inf, dict):
        return ["realization infrastructure must be a mapping"]
    hub = []
    raw = inf.get("hub") or []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    for v in raw:
        try:
            hub.append(ipaddress.ip_network(str(v).strip(), strict=True))
        except ValueError as e:
            errors.append(f"realization infrastructure.hub: {v}: {e}")
    eg = real.get("egress") or {}
    for k in ("platformSources", "platformPorts"):
        if k in eg:
            errors.append(f"realization egress.{k} is replaced by infrastructure.hub and service.ports: remove it")
    for key, n in realization_pools(real):
        for h in hub:
            if h.version == n.version and h.overlaps(n):
                errors.append(f"realization {key} {n} overlaps infrastructure.hub {h}")
    sv = real.get("service") or {}
    if not isinstance(sv, dict):
        return errors + ["realization service must be a mapping"]
    if not bool(sv.get("enabled")):
        return errors
    if not (real.get("attachments") or {}).get("enabled"):
        errors.append("realization service.enabled needs attachments.enabled (the platform circuits)")
    hub4 = [h for h in hub if h.version == 4]
    if not hub4:
        errors.append("realization service.enabled needs an IPv4 range in infrastructure.hub")
    vrf = sv.get("vrf") or {}
    name = str(vrf.get("name") or "")
    ev = eg.get("vrf") or {}
    if not name:
        errors.append("realization service.vrf.name is required")
    elif not SERVICE_NAME.match(name) or re.fullmatch(r"e\d+", name) or name == str(ev.get("name") or "egress") \
            or re.fullmatch(r"(se|so|xe|xo)\d+", name) or name in ("egress0", "lo"):
        errors.append(f"realization service.vrf.name {name!r} must be a device name of at most 15 characters, other "
                      "than the egress VRF's and not of the form e<digits>")
    try:
        table = int(vrf.get("table"))
    except (TypeError, ValueError):
        table = -1
    base = int(eg.get("networkTableBase") or 100000)
    if table < 1 or table in EGRESS_RESERVED_TABLES or table == int(ev.get("table") or 9000) or table >= base:
        errors.append(f"realization service.vrf.table {vrf.get('table')!r} must be a table other than 253-255 and the "
                      f"egress VRF's, below egress.networkTableBase {base}")
    _ports(sv.get("ports"), "service.ports", errors)
    for i, ep in enumerate(sv.get("hubEndpoints") or []):
        n = _net((ep or {}).get("prefix")) if isinstance(ep, dict) else None
        if n is None or n.version != 4:
            errors.append(f"realization service.hubEndpoints[{i}].prefix {(ep or {}).get('prefix') if isinstance(ep, dict) else ep!r} "
                          "is not an IPv4 network")
        elif not any(n.subnet_of(h) for h in hub4):
            errors.append(f"realization service.hubEndpoints[{i}].prefix {n} is not inside infrastructure.hub")
        _ports((ep or {}).get("ports") if isinstance(ep, dict) else None, f"service.hubEndpoints[{i}].ports", errors)
    rows = [(n, purpose, rule, "") for key, n, purpose, rule, site in range_rows(real) if not site]
    if not sv.get("attachableRanges"):
        errors.append("realization service.attachableRanges is required with service.enabled: the IPv4 ranges a "
                      "cluster's prefixes must lie in, announced to the hub's network (0.0.0.0/0 admits every range "
                      "the other checks admit)")
    for i, v in enumerate(sv.get("attachableRanges") or []):
        n = _net(v)
        if n is None or n.version != 4:
            errors.append(f"realization service.attachableRanges[{i}] {v!r} is not an IPv4 network")
            continue
        if n.prefixlen == 0:
            # everything, written explicitly: each prefix is still checked against the reserved ranges
            continue
        clash = range_clash(n, rows)
        if clash is not None:
            errors.append(f"realization service.attachableRanges[{i}] {n} overlaps {clash[0]} ({lower_first(clash[1])})")
    return errors


def router_advertisement_check(real):
    """placement.routerAdvertisementBySite (check-fabric): per site name a mapping whose url is an https:// URL of the
    site's router advertisement service (an empty map: no site has one)."""
    by = ((real or {}).get("placement") or {}).get("routerAdvertisementBySite")
    if by in (None, {}):
        return []
    if not isinstance(by, dict):
        return ["realization placement.routerAdvertisementBySite must be a mapping of site names to {url}"]
    errors = []
    for site, entry in sorted(by.items(), key=lambda kv: str(kv[0])):
        key = f"placement.routerAdvertisementBySite[{site}].url"
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip():
            errors.append(f"realization {key} is required (the site's router advertisement service)")
        elif not re.fullmatch(r"https://[^\s/?#]+(/\S*)?", url.strip()):
            errors.append(f"realization {key} must be an https:// URL, not {url!r}")
    return errors


def service_settings(real):
    """The service VRF's settings as the plan uses them (after service_check): {vrf, table, hub (IPv4, collapsed),
    hub6, ports, hubEndpoints [{prefix, ports}], attachable}."""
    sv = (real or {}).get("service") or {}
    vrf = sv.get("vrf") or {}
    hub4 = sorted(ipaddress.collapse_addresses(hub_nets(real, 4)), key=lambda n: (int(n.network_address), n.prefixlen))
    eps = []
    for ep in sv.get("hubEndpoints") or []:
        n = _net((ep or {}).get("prefix"))
        if n is not None and n.version == 4:
            eps.append({"prefix": str(n), "ports": sorted({int(p) for p in (ep.get("ports") or [])})})
    att = [n for n in (_net(v) for v in sv.get("attachableRanges") or []) if n is not None and n.version == 4]
    return {"vrf": str(vrf.get("name") or "service"), "table": int(vrf.get("table") or 9001),
            "hub": [str(n) for n in hub4], "ports": sorted({int(p) for p in sv.get("ports") or [6443, 443]}),
            "hubEndpoints": eps, "attachable": [str(n) for n in att]}


def service_status_off():
    return {"enabled": False, "vrf": "", "sources": [], "ports": [], "hubEndpoints": [], "attachableRanges": [],
            "circuits": [], "networks": [], "refused": []}


def network_space4(n):
    """A HybridNetwork object's IPv4 address space: status.addressSpace (what the network job accepted), spec.prefixes
    only while the status holds none (a prefix the tenant wrote but the job refused never counts)."""
    out = []
    st = n.get("status") or {}
    held = [a.get("prefix") for a in st.get("addressSpace") or [] if isinstance(a, dict)]
    for c in (held or ((n.get("spec") or {}).get("prefixes") or [])):
        x = _net(c) if c is not None else None
        if x is not None and x.version == 4 and x not in out:
            out.append(x)
    return out


def prefix_verdict(text, space, rows, attachable):
    """A service prefix on its own (no other network considered): (network or None, reason, message); reason "" =
    acceptable. space: the network's IPv4 space (networks); rows: range rows (network, purpose, rule, site) without
    site rows; attachable: networks (empty = any)."""
    try:
        net = ipaddress.ip_network(str(text).strip(), strict=True)
    except ValueError:
        return None, "OutsideAddressSpace", f"{text} is not an address prefix"
    if net.version != 4:
        return net, "FamilyUnavailable", f"{net}: the platform reaches clusters over IPv4 only"
    if not any(net.subnet_of(s) for s in space):
        return net, "OutsideAddressSpace", f"{net} is not inside the network's IPv4 address space"
    clash = range_clash(net, rows)
    if clash is not None:
        return net, "PrefixReserved", reserved_message("prefix", net, clash)
    if attachable and not any(net.subnet_of(a) for a in attachable):
        return net, "OutsideAttachableRanges", (f"{net} is outside the ranges the platform reaches "
                                                f"({', '.join(str(a) for a in attachable)})")
    return net, "", ""


def service_prefix_check(req):
    """One network's service prefixes (service-prefix-check, for the network job): {prefixes: [{prefix, cluster?}],
    space: [the network's prefixes], realization (or ranges: published rows, and attachableRanges), accepted: [the
    prefixes accepted for other networks]} -> rows {prefix, cluster, state (pending|refused), reason, message}: each
    prefix inside the network's IPv4 space, clear of the reserved ranges, inside service.attachableRanges (when set) and
    overlapping no prefix accepted for another network (ServicePrefixInUse). The ledger settles who came first; this
    only says whether a prefix can be accepted now."""
    real = req.get("realization")
    if real is not None:
        rows = [(n, p, r, "") for _, n, p, r, s in range_rows(real) if not s]
        attachable = [ipaddress.ip_network(a) for a in service_settings(real)["attachable"]]
    else:
        rows = [r for r in rows_of(req.get("ranges")) if not r[3]]
        attachable = [n for n in (_net(a) for a in req.get("attachableRanges") or []) if n is not None and n.version == 4]
    space = [n for n in (_net(c) for c in req.get("space") or []) if n is not None and n.version == 4]
    others = [n for n in (_net(c) for c in req.get("accepted") or []) if n is not None]
    out = []
    for p in req.get("prefixes") or []:
        p = p if isinstance(p, dict) else {"prefix": p}
        net, reason, msg = prefix_verdict(p.get("prefix"), space, rows, attachable)
        if not reason and any(net.overlaps(o) for o in others if o.version == net.version):
            reason, msg = "ServicePrefixInUse", f"{net} is already in use by another network on the platform's service routing domain"
        out.append({"prefix": str(net) if net is not None else str(p.get("prefix")), "cluster": str(p.get("cluster") or ""),
                    "state": "refused" if reason else "pending", "reason": reason, "message": msg})
    return {"errors": [], "prefixes": out}


def accepted_row(row):
    """A row of HybridNetwork status.platformAccess.prefixes the network accepted into the service VRF: state ready,
    or state pending with no reason."""
    state = str(row.get("state") or "")
    return state == "ready" or (state == "pending" and not str(row.get("reason") or ""))


def service_link(pool, index):
    """The service link of root index i: the i-th /31 of the link pool's upper half (so even, se odd)."""
    upper = int(pool.network_address) + pool.num_addresses // 2
    so = ipaddress.ip_address(upper + 2 * index)
    return so, so + 1


def service_plan(req):
    """The service VRF on the border gateway nodes' root instance (service-plan, see the module docstring)."""
    real = req.get("realization") or {}
    sv = real.get("service") or {}
    node_names = req.get("nodes") or []
    vrfs_in = req.get("vrfs") or {}
    if not sv.get("enabled"):
        return {"errors": [], "enabled": False, "nodes": {}, "status": service_status_off(),
                "vrfs": {n: list(vrfs_in.get(n) or []) for n in node_names}, "pending": []}
    errors = service_check(real)
    if errors:
        return {"errors": errors, "enabled": True, "nodes": {}, "status": service_status_off(), "vrfs": vrfs_in,
                "pending": []}
    cfg = service_settings(real)
    eg, at, core = real.get("egress") or {}, real.get("attachments") or {}, real.get("core") or {}
    base = int(eg.get("networkTableBase") or 100000)
    pool = ipaddress.ip_network(str(eg.get("linkPool") or "169.254.0.0/16"), strict=True)
    slots = pool.num_addresses // 4
    asn = int(req.get("domainAsn") or 0)
    fabric = req.get("fabric")
    alloc = req.get("ledger") or {}
    rows = [(n, p, r, "") for _, n, p, r, s in range_rows(real) if not s]
    attachable = [ipaddress.ip_network(a) for a in cfg["attachable"]]
    prev = {(str(w.get("network")), str(p)) for w in ((req.get("previous") or {}).get("networks") or [])
            for p in w.get("prefixes") or []}
    sprefix = alloc.get("servicePrefix") or {}
    pending, refused, cands = [], [], []
    nets = {}
    for n in sorted(req.get("networks") or [], key=lambda o: ((o.get("metadata") or {}).get("namespace", ""),
                                                             (o.get("metadata") or {}).get("name", ""))):
        md, st = n.get("metadata") or {}, n.get("status") or {}
        key = f"{md.get('namespace')}/{md.get('name')}"
        owner = f"HybridNetwork/{key}"
        if st.get("fabric") != fabric:
            continue
        # the rows the network accepted: ready, or pending without a reason (a pending row with a reason waits for
        # the network itself, e.g. its platform access is not available yet or its ledger entry is not written)
        rows_in = [r for r in ((st.get("platformAccess") or {}).get("prefixes") or []) if isinstance(r, dict)
                   and r.get("prefix") and accepted_row(r)]
        if not rows_in:
            continue
        t, i = (alloc.get("vpnTarget") or {}).get(owner), (alloc.get("egress") or {}).get(owner)
        if t is None or i is None:
            pending.append({"network": key, "why": "the network holds no VPN target or root index yet"})
            continue
        if not 0 <= int(i) < slots:
            pending.append({"network": key, "why": f"root index {i} is outside the service links (0-{slots - 1})"})
            continue
        space = network_space4(n)
        nets[key] = {"owner": owner, "vpnTarget": int(t), "index": int(i)}
        seen = set()
        for r in rows_in:
            net, reason, msg = prefix_verdict(r["prefix"], space, rows, attachable)
            text = str(net) if net is not None else str(r["prefix"])
            if text in seen:
                continue
            seen.add(text)
            if reason:
                refused.append({"network": key, "prefix": text, "reason": reason, "message": msg})
                continue
            led = sprefix.get(f"{owner}/{text}")
            if led is None:
                pending.append({"network": key, "prefix": text, "why": "not recorded in the fabric's ledger yet"})
                continue
            cands.append({"network": key, "net": net, "text": text, "order": (int(led), (key, text) not in prev, key, text)})
    # first come first served: the ledger's order (the networks keep a prefix's entry while a cluster asks for it,
    # through any outage of the platform's side; the previous status is only a tie-break, it is empty after a run
    # without a platform circuit)
    accepted = {}
    for c in sorted(cands, key=lambda c: c["order"]):
        if any(o["net"].overlaps(c["net"]) for k, lst in accepted.items() if k != c["network"] for o in lst):
            refused.append({"network": c["network"], "prefix": c["text"], "reason": "ServicePrefixInUse",
                            "message": f"{c['text']} is already in use by another network on the platform's service "
                                       "routing domain"})
            continue
        accepted.setdefault(c["network"], []).append(c)
    circuits = [c for c in req.get("circuits") or [] if c.get("nodes")]
    with_circuit = {n for c in circuits for n in c["nodes"]}
    networks, status_nets = [], []
    for key in sorted(accepted, key=lambda k: nets[k]["vpnTarget"]):
        w = nets[key]
        t, i = w["vpnTarget"], w["index"]
        so, se = service_link(pool, i)
        prefixes = sorted({x["net"] for x in accepted[key]}, key=lambda n: (int(n.network_address), n.prefixlen))
        entry = {"network": key, "vpnTarget": t, "index": i, "vrf": f"e{t}", "table": base + t,
                 "se": f"se{t}", "so": f"so{t}", "seAddr": f"{se}/31", "soAddr": f"{so}/31", "seIp": str(se),
                 "soIp": str(so), "prefixes": [str(p) for p in prefixes],
                 "rd": f"{asn}:{t}" if 0 < asn <= 65535 else ""}
        networks.append(entry)
        if with_circuit:
            status_nets.append({"network": key, "vpnTarget": t, "index": i, "prefixes": entry["prefixes"]})
    all_prefixes = sorted({p for w in networks for p in w["prefixes"]},
                          key=lambda s: (int(ipaddress.ip_network(s).network_address), ipaddress.ip_network(s).prefixlen))

    def le32(n):
        n = ipaddress.ip_network(str(n))
        return entry_text(n, le=32) if n.prefixlen < 32 else str(n)
    deny = ["0.0.0.0/0"] + [le32(c) for c in (at.get("linkPool"), core.get("loopbackPool"), core.get("linkSubnet"),
                                               str(pool)) if _net(c) is not None and _net(c).version == 4] \
        + [le32(p) for p in all_prefixes]
    accept = [le32(h) for h in cfg["hub"]]
    attach = [le32(a) for a in cfg["attachable"]]
    policy = hashlib.sha1(json.dumps([deny, accept, all_prefixes, attach], sort_keys=True).encode()).hexdigest()[:12]
    st_circuits = [{"circuit": c["circuit"], "vlan": c["vlan"], "nodes": sorted(c["nodes"]), "rev": c.get("rev", ""),
                    "policy": policy, "device": c["device"]} for c in circuits]
    nodes = {}
    mtu = int(eg.get("mtu") or 9000)
    for name in node_names:
        mine = [c for c in circuits if name in c["nodes"]]
        if not mine:
            continue
        nodes[name] = {"vrf": cfg["vrf"], "table": cfg["table"], "hub": cfg["hub"], "ports": cfg["ports"],
                       "hubEndpoints": cfg["hubEndpoints"], "mtu": mtu, "deny": deny, "accept": accept,
                       "advertise": [str(p) for p in all_prefixes], "attachable": attach,
                       "circuits": [dict({k: v for k, v in c.items() if k != "nodes"},
                                         address=f"{c['nodes'][name]}/{ipaddress.ip_network(c['link']).prefixlen}",
                                         ip=c["nodes"][name])
                                    for c in mine],
                       "networks": networks}
    # the root VRFs per node, with the service link of each network that has accepted prefixes there
    vrfs = {}
    for name in node_names:
        by_vrf = {v["vrf"]: dict(v) for v in vrfs_in.get(name) or []}
        if name in nodes:
            for w in networks:
                link = {k: w[k] for k in ("se", "so", "seAddr", "soAddr", "seIp", "soIp", "prefixes")}
                v = by_vrf.setdefault(w["vrf"], {"network": w["network"], "vpnTarget": w["vpnTarget"], "vrf": w["vrf"],
                                                 "table": w["table"], "nat": "", "egress": False, "rd": w["rd"],
                                                 "circuits": []})
                v["service"] = link
        vrfs[name] = sorted(by_vrf.values(), key=lambda v: int(v["vpnTarget"]))
    status = {"enabled": True, "vrf": cfg["vrf"], "sources": cfg["hub"], "ports": cfg["ports"],
              "hubEndpoints": cfg["hubEndpoints"], "attachableRanges": cfg["attachable"], "circuits": st_circuits,
              "networks": status_nets,
              "refused": sorted(refused, key=lambda r: (r["network"], r["prefix"]))}
    return {"errors": [], "enabled": True, "nodes": nodes, "status": status, "vrfs": vrfs, "pending": pending}


# ------------------------------------------------------------------------------------------- cluster networks
def check_cluster_networks(req):
    """A cluster's own networks (check-cluster-networks): {clusterNetwork, serviceNetwork, machineNetwork, provider
    (openstack|aws|gcp), network (the cluster names a hybrid network), space: [the hybrid network's prefixes], ranges
    (published rows) or realization (+ sites), site (the environment's site: its own rows count)}. clusterNetwork and
    serviceNetwork must not overlap each other, the machine network, the network's space or a reserved range; an
    OpenStack machine network not the network's space or a reserved range; an AWS or Google Cloud machine network on a
    network (the placement's subnet) not a reserved range. -> {errors, reason (PrefixReserved, AddressConflict,
    InvalidSpec or ""), reserved: [...], conflicts: [...]}; a reserved range is named with its purpose."""
    if req.get("ranges") is not None:
        rows = rows_of(req.get("ranges"))
    else:
        rows = [(n, p, r, s) for _, n, p, r, s in range_rows(req.get("realization") or {}, req.get("sites") or [])]
    site = str(req.get("site") or "")
    provider = str(req.get("provider") or "openstack")
    on_network = bool(req.get("network"))
    space = [n for n in (_net(c) for c in req.get("space") or []) if n is not None]
    nets, invalid = {}, []
    for f in ("clusterNetwork", "serviceNetwork", "machineNetwork"):
        v = req.get(f)
        if v in (None, ""):
            continue
        n = _net(v)
        if n is None or n.version != 4:
            invalid.append(f"spec.networking.{f} {v!r} is not a canonical IPv4 network")
        else:
            nets[f] = n
    if invalid:
        return {"errors": invalid, "reason": "InvalidSpec", "reserved": [], "conflicts": []}
    reserved, conflicts = [], []

    def clash_with_ranges(f):
        c = None
        for n, purpose, rule, s in rows:
            if s and s != site:
                continue
            if n.version == nets[f].version and n.overlaps(nets[f]):
                c = (n, purpose, rule)
                break
        if c is not None:
            reserved.append(reserved_message(f, nets[f], c))

    for f in ("clusterNetwork", "serviceNetwork"):
        if f not in nets:
            continue
        for g in ("clusterNetwork", "serviceNetwork", "machineNetwork"):
            if g != f and g in nets and nets[f].overlaps(nets[g]):
                pair = tuple(sorted((f, g)))
                msg = f"spec.networking.{pair[0]} {nets[pair[0]]} overlaps spec.networking.{pair[1]} {nets[pair[1]]}"
                if msg not in conflicts:
                    conflicts.append(msg)
        for s in space:
            if s.version == 4 and s.overlaps(nets[f]):
                conflicts.append(f"spec.networking.{f} {nets[f]} overlaps the network's address space {s}")
        clash_with_ranges(f)
    if "machineNetwork" in nets:
        if provider == "openstack":
            for s in space:
                if s.version == 4 and s.overlaps(nets["machineNetwork"]):
                    conflicts.append(f"spec.networking.machineNetwork {nets['machineNetwork']} overlaps the network's "
                                     f"address space {s}")
            clash_with_ranges("machineNetwork")
        elif on_network:
            clash_with_ranges("machineNetwork")
    reason = "PrefixReserved" if reserved else ("AddressConflict" if conflicts else "")
    return {"errors": reserved + conflicts, "reason": reason, "reserved": reserved, "conflicts": conflicts}


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
    # IPv6 on circuits (optional): a /64 per circuit by its link index, so the pool is at least a /64; it and the
    # platform's own IPv6 ranges (reserved6) are IPv6 networks, and the pool stays clear of the core's IPv6 ranges
    if str(at.get("linkPool6") or "").strip():
        try:
            pool6 = ipaddress.ip_network(str(at.get("linkPool6")), strict=True)
            if pool6.version != 6:
                errors.append(f"realization attachments.linkPool6 {pool6} is not an IPv6 network")
            elif pool6.prefixlen > 64:
                errors.append(f"realization attachments.linkPool6 {pool6} is too small (at least a /64)")
            else:
                for k in ("loopbackPool6", "icmpSource6"):
                    try:
                        c = core6(core or {}, k, 128)
                    except ValueError:
                        continue
                    if c is not None and pool6.overlaps(c):
                        errors.append(f"realization attachments.linkPool6 {pool6} overlaps core.{k} {c}")
                try:
                    c = srv6_block(core)
                except ValueError:
                    c = None  # srv6_check names it
                if c is not None and pool6.overlaps(c):
                    errors.append(f"realization attachments.linkPool6 {pool6} overlaps core.srv6.block {c}")
        except ValueError as e:
            errors.append(f"realization attachments.linkPool6: {e}")
    for c in at.get("reserved6") or []:
        try:
            if ipaddress.ip_network(str(c), strict=True).version != 6:
                errors.append(f"realization attachments.reserved6: {c} is not an IPv6 network")
        except ValueError as e:
            errors.append(f"realization attachments.reserved6: {e}")
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
    out = {"errors": [], "link": str(link), "prefixLength": plen,
           "nodes": [{"name": n, "address": str(hosts[i])} for i, n in enumerate(names)],
           "ce": str(hosts[len(names)]), "ceSecond": str(hosts[len(names) + 1])}
    if req.get("ipv6"):
        # the IPv6 link: the index-th /64 of linkPool6 (a /64 keeps neighbour discovery plain on CE gear), the same
        # host numbers as the IPv4 link; no ledger entry of its own
        try:
            pool6 = ipaddress.ip_network(str(at.get("linkPool6") or ""), strict=True)
        except ValueError:
            pool6 = None
        if pool6 is None or pool6.version != 6 or pool6.prefixlen > 64:
            return {"errors": [NO_CIRCUIT_IPV6], "reason": "FamilyUnavailable"}
        if index >= 2 ** (64 - pool6.prefixlen):
            return {"errors": ["no IPv6 link is left for this circuit"], "reason": "FamilyUnavailable"}
        link6 = ipaddress.ip_network((int(pool6.network_address) + (index << 64), 64))
        a6 = link6.network_address
        out["ipv6"] = {"link": str(link6), "prefixLength": 64,
                       "nodes": [{"name": n, "address": str(a6 + i + 1)} for i, n in enumerate(names)],
                       "ce": str(a6 + len(names) + 1), "ceSecond": str(a6 + len(names) + 2)}
    return out


ACCEPT_RE = re.compile(r"^(\S+)(?: ge (\d+))?(?: le (\d+))?$")
NO_CIRCUIT_IPV6 = "IPv6 is not offered on attachment circuits of this fabric"


def platform_ranges6(at, core, translation, hub=None):
    """The platform's own IPv6 ranges a CE's routes never reach into: the circuits' IPv6 link pool
    (attachments.linkPool6), the installation's IPv6 infrastructure ranges (attachments.reserved6), the core's IPv6
    loopbacks, ICMPv6 sources and SRv6 SID block (core.loopbackPool6, core.icmpSource6, core.srv6.block) and every
    site's translation prefix (translation.<site>.pref64). Without those inside another one, in address order;
    unusable values are left out (the realization check names them)."""
    srv6 = (core or {}).get("srv6")
    given = [(at or {}).get("linkPool6")] + list((at or {}).get("reserved6") or []) + [
        (core or {}).get("loopbackPool6"), (core or {}).get("icmpSource6"),
        srv6.get("block") if isinstance(srv6, dict) else None]
    given += [((translation or {}).get(s) or {}).get("pref64") for s in sorted((translation or {}).keys())]
    # the IPv6 ranges of the networks the platform's management connects from (infrastructure.hub)
    given += list(hub or [])
    out = []
    for c in given:
        if not str(c or "").strip():
            continue
        try:
            n = ipaddress.ip_network(str(c), strict=True)
        except ValueError:
            continue
        if n.version == 6:
            out.append(n)
    return sorted(collapse(out, 6), key=lambda n: (int(n.network_address), n.prefixlen))


def check_circuit(req):
    """A circuit's accept list against the network's address space and the platform's ranges (reserved, and for IPv6
    platform_ranges6). An entry inside the space or a platform range is refused; an entry that covers part of one
    only gets a note (the edge drops overlapping routes regardless). IPv6 entries also need IPv6 on the fabric's
    circuits (attachments.linkPool6) and IPv6 address space on the network (familyErrors). acceptsDefault /
    acceptsDefault6: an entry matches 0.0.0.0/0 / ::/0; ipv6: the list has IPv6 entries."""
    if (req.get("ceKind") or "customer") == "platform":
        return check_platform_circuit(req)
    errors, family_errors, notes, accepts_default, accepts_default6 = [], [], [], False, False
    space = []
    for p in req.get("space") or []:
        try:
            n = ipaddress.ip_network(str(p), strict=True)
        except ValueError:
            continue
        if n not in space:
            space.append(n)
    reserved = []
    for r in req.get("reserved") or []:
        try:
            reserved.append(ipaddress.ip_network(str(r), strict=False))
        except ValueError:
            continue
    at = req.get("attachments") or {}
    reserved += platform_ranges6(at, req.get("core"), req.get("translation"), req.get("hub6"))
    entries = []
    for e in req.get("accept") or []:
        a = accept_entry(e)
        if a is None:
            errors.append(f"accept entry '{e}' is not '<prefix>[ ge <n>][ le <n>]'")
            continue
        entries.append((e, a))
    ipv6 = any(a[0].version == 6 for _, a in entries)
    if ipv6 and not str(at.get("linkPool6") or "").strip():
        family_errors.append(NO_CIRCUIT_IPV6)
    elif ipv6 and not any(q.version == 6 for q in space):
        family_errors.append("the network has no IPv6 address space")
    for e, (net, ge, le) in entries:
        if net.prefixlen == 0 and ge is None:
            if net.version == 4:
                accepts_default = True
            else:
                accepts_default6 = True
        for q in space:
            if q.version != net.version:
                continue
            if net.subnet_of(q):
                errors.append(f"accept entry '{e}' lies inside the network's address space {q}")
            elif q.subnet_of(net):
                notes.append(f"routes overlapping the network's address space {q} are never accepted from the CE")
        for q in reserved:
            if q.version != net.version:
                continue
            if net.subnet_of(q):
                errors.append(f"accept entry '{e}' lies inside the platform range {q}")
            elif q.subnet_of(net) and net.prefixlen > 0:
                notes.append(f"routes overlapping the platform range {q} are never accepted from the CE")
    # the published ranges, named with their purpose: an entry inside a Refused or SteppedAround range is refused, one
    # covering part of a Refused range only gets a note (as above)
    reserved_errors = []
    for e, (net, ge, le) in entries:
        for q, purpose, rule, site in rows_of(req.get("ranges")):
            if site or q.version != net.version:
                continue
            if net.subnet_of(q):
                reserved_errors.append(f"accept entry '{e}' lies inside {q}, reserved by the platform: {lower_first(purpose)}")
            elif rule == REFUSED and q.subnet_of(net) and net.prefixlen > 0:
                notes.append(f"routes overlapping {q} ({lower_first(purpose)}) are never accepted from the CE")
    errors += reserved_errors
    egress = bool(req.get("egressInternet"))
    if accepts_default and egress:
        notes.append("0.0.0.0/0 is not accepted from the CE while the network has provider internet egress")
    if req.get("advertiseDefault") and not egress:
        notes.append("no default route is advertised: the network has no provider internet egress")
    egress6 = bool(req.get("egressInternet6"))
    if accepts_default6:
        # the site gateways keep their own IPv6 default route (the egress default, or the fail-fast one without
        # egress), so a CE's would never be used: never accepted
        notes.append("::/0 is never accepted from the CE")
    if ipv6 and req.get("advertiseDefault") and not egress6:
        notes.append("no IPv6 default route is advertised: the network has no IPv6 internet egress")
    out = {"errors": errors, "familyErrors": family_errors, "notes": sorted(set(notes)),
           "acceptsDefault": accepts_default, "acceptsDefault6": accepts_default6, "ipv6": ipv6}
    if req.get("ranges") is not None:
        out["reason"] = "PrefixReserved" if reserved_errors else ""
    return out


def check_platform_circuit(req):
    """A circuit of ceKind platform (check-circuit with ceKind platform): {namespace, fabricNamespace, accept,
    realization (service, infrastructure), family?: [4|6]}. Refused (errors, reason): outside the fabric's namespace
    (NotPlatformNamespace), without the service VRF (ServiceNotOffered), with accept entries (InvalidSpec: a platform
    circuit accepts the hub's ranges only), with IPv6 (FamilyUnavailable). ceConfig: advertisedRanges (each
    service.attachableRanges entry 'le 32': the routes the platform may announce, never more) and acceptedRanges
    (each IPv4 infrastructure.hub range 'le 32'); notes: routing.advertise is not used."""
    real = req.get("realization") or {}
    sv = real.get("service") or {}
    out = {"errors": [], "familyErrors": [], "notes": [], "acceptsDefault": False, "acceptsDefault6": False,
           "ipv6": False, "reason": "", "ceConfig": {}}

    def refuse(reason, msg):
        out["errors"].append(msg)
        out["reason"] = out["reason"] or reason
    if req.get("namespace") != req.get("fabricNamespace"):
        refuse("NotPlatformNamespace", "a circuit of ceKind platform is only offered in the platform's namespace")
    if not sv.get("enabled"):
        refuse("ServiceNotOffered", "the fabric offers no service routing domain for platform circuits")
    if req.get("accept"):
        refuse("InvalidSpec", "a platform circuit accepts the platform's infrastructure ranges; routing.accept is not "
                              "allowed")
    if 6 in [int(f) for f in req.get("family") or [] if str(f).isdigit()]:
        refuse("FamilyUnavailable", "a platform circuit carries IPv4 only")
        out["familyErrors"].append("a platform circuit carries IPv4 only")
    if req.get("advertise"):
        out["notes"].append("routing.advertise is not used: a platform circuit announces the clusters' prefixes only")
    hub4 = sorted(ipaddress.collapse_addresses(hub_nets(real, 4)), key=lambda n: (int(n.network_address), n.prefixlen))
    att = [n for n in (_net(v) for v in sv.get("attachableRanges") or []) if n is not None and n.version == 4]
    out["ceConfig"] = {"advertisedRanges": [entry_text(n, le=32) if n.prefixlen < 32 else str(n) for n in att],
                       "acceptedRanges": [entry_text(n, le=32) if n.prefixlen < 32 else str(n) for n in hub4]}
    return out


def accept_entry(e):
    """A prefix-list entry "<prefix>[ ge <n>][ le <n>]" of either family as (network, ge, le), or None when malformed
    (ge and le longer than the prefix and at most 32 or 128, ge not above le)."""
    m = ACCEPT_RE.match(str(e).strip())
    try:
        net = ipaddress.ip_network(m.group(1), strict=True) if m else None
    except ValueError:
        return None
    if net is None or "%" in m.group(1):
        return None
    top = net.max_prefixlen
    ge, le = (int(m.group(2)) if m.group(2) else None), (int(m.group(3)) if m.group(3) else None)
    if (ge is not None and not net.prefixlen < ge <= top) or (le is not None and not net.prefixlen < le <= top) \
            or (ge is not None and le is not None and ge > le):
        return None
    return net, ge, le


def collapse(networks, version=4):
    """The networks of one family (IPv4 unless version says otherwise) without those inside another one (duplicates
    once)."""
    fam = sorted({n for n in networks if n.version == version}, key=lambda n: n.prefixlen)
    out = []
    for n in fam:
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
    errors, skipped, circuits, status, platform = [], [], [], [], []
    nodes = {n: {"circuits": []} for n in node_names}
    if not at.get("enabled"):
        return {"errors": [], "circuits": [], "skipped": [], "nodes": {}, "status": [],
                "vrfs": root_vrfs(req, [])}
    service = req.get("service") or {}
    service_on = bool(service.get("enabled"))
    vp = at.get("vlanPool") or {}
    pool = ipaddress.ip_network(str(at.get("linkPool")), strict=True)
    reserved = []
    for r in req.get("reserved") or []:
        try:
            reserved.append(ipaddress.ip_network(str(r), strict=True))
        except ValueError as e:
            errors.append(f"reserved range {r}: {e}")
    sources = [str(ipaddress.ip_network(str(c), strict=True)) for c in req.get("platformSources") or []]
    reserved6 = platform_ranges6(at, req.get("core"), req.get("translation"), req.get("hub6"))
    try:
        pool6 = ipaddress.ip_network(str(at.get("linkPool6") or ""), strict=True)
    except ValueError:
        pool6 = None
    nets_by_key, space = {}, {}
    for n in req.get("networks") or []:
        md = n.get("metadata") or {}
        k = f"{md.get('namespace')}/{md.get('name')}"
        nets_by_key[k] = n
        # the space as asked for and as the IPAM holds it (they differ while an edit is refused); IPv6 also the
        # network's IPv6 space in effect and its platform block
        v6 = (n.get("status") or {}).get("ipv6") or {}
        for c in ((n.get("spec") or {}).get("prefixes") or []) + [
                a.get("prefix") for a in (n.get("status") or {}).get("addressSpace") or []] + list(
                v6.get("prefixes") or []) + [(v6.get("block") or {}).get("prefix")]:
            if c is None:
                continue
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
        if (sp.get("ceKind") or "customer") == "platform":
            # a platform circuit: the hand-off of the service VRF to the hub's network (service-plan gives it its
            # routing policy); never in a network's VRF, never in the circuits' filter table
            if not service_on:
                skip("the fabric offers no service VRF (realization service.enabled)")
                continue
            if md.get("namespace") != req.get("fabricNamespace"):
                skip("a platform circuit lives in the fabric's namespace only")
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
            if (r.get("authentication") or "md5") == "md5" and key not in passwords:
                skip("no usable BGP password in the platform's secret store")
                continue
            addrs = {n.get("name"): n.get("address") for n in r.get("nodes") or []}
            vlans[vlan] = key
            parents.add(parent)
            platform.append({"circuit": key, "kind": "platform", "id": f"AC{vlan}",
                             "vrf": str((service.get("vrf") or {}).get("name") or "service"),
                             "table": int((service.get("vrf") or {}).get("table") or 9001), "vlan": vlan,
                             "device": device, "parent": parent, "mtu": int(r.get("mtu") or 1500), "link": str(link),
                             "ce": str(r.get("ce")), "peerAsn": int(r["peerAsn"]),
                             "authentication": r.get("authentication") or "md5",
                             "passwordPath": r.get("passwordPath") or "", "maxPrefixes": int(r.get("maxPrefixes") or 100),
                             "maxPrefixAction": r.get("maxPrefixAction") or "teardown", "bfd": bool(r.get("bfd")),
                             "rev": str(r.get("rev") or ""),
                             "nodes": {n: addrs[n] for n in node_names if addrs.get(n)}})
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
        accept, accept6, nets6, bad = [], [], [], []
        for e in r.get("accept") or []:
            a = accept_entry(e)
            if a is None:
                bad.append(str(e))
            elif a[0].version == 6:
                accept6.append(entry_text(*a))
                nets6.append(a[0])
            else:
                accept.append(entry_text(*a))
        if bad:
            skip(f"malformed accept entries: {', '.join(bad)}")
            continue
        # IPv6 accept entries: the circuit's IPv6 link (the circuit job numbers it from linkPool6) and every node's
        # address on it
        if accept6:
            try:
                link6 = ipaddress.ip_network(str(r.get("link6")), strict=True)
                ce6 = ipaddress.ip_address(str(r.get("ce6")))
                addrs6 = {n.get("name"): ipaddress.ip_address(str(n.get("address6")))
                          for n in r.get("nodes") or [] if n.get("address6")}
            except ValueError:
                link6 = None
            if link6 is None or pool6 is None or link6.version != 6 or not link6.subnet_of(pool6) \
                    or link6.prefixlen != 64 or ce6 not in link6 or not all(a in link6 for a in addrs6.values()):
                skip(f"IPv6 accept entries without a usable IPv6 link ({r.get('link6') or 'none'}) in the IPv6 link "
                     f"pool ({at.get('linkPool6') or 'none'})")
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
        if accept6 and any(n not in addrs6 for n in node_names if addrs.get(n)):
            skip("no IPv6 address for every node on the IPv6 link")
            continue
        plan = {"circuit": key, "network": net_key, "id": f"AC{vlan}", "vrf": f"e{t}", "table": base + t,
                "vpnTarget": t, "zone": zone, "vlan": vlan, "device": device, "parent": parent,
                "mtu": int(r.get("mtu") or 1500), "link": str(link), "ce": str(r.get("ce")),
                "peerAsn": int(r["peerAsn"]), "authentication": r.get("authentication") or "md5",
                "passwordPath": r.get("passwordPath") or "", "maxPrefixes": int(r.get("maxPrefixes") or 100),
                "maxPrefixAction": r.get("maxPrefixAction") or "teardown", "bfd": bool(r.get("bfd")),
                "accept": accept, "deny": deny, "advertise": advertise, "denyDefault": internet_intent,
                "advertiseDefault": nat, "nat": nat, "rev": str(r.get("rev") or ""),
                "nodes": {n: addrs[n] for n in node_names if addrs.get(n)}}
        hashed = [deny, accept, advertise, nat]
        if accept6:
            # IPv6: the same policy per family; the default route only where the network has IPv6 internet egress
            egress6 = bool(eg.get("ipv6"))
            mine6 = sorted({n for n in prefixes.get(net_key, []) if n.version == 6},
                           key=lambda n: (int(n.network_address), n.prefixlen))
            own6 = sorted(collapse(space.get(net_key, []) + mine6, 6),
                          key=lambda n: (int(n.network_address), n.prefixlen))
            deny6 = [entry_text(n, le=128) if n.prefixlen < 128 else str(n) for n in own6 + reserved6] + ["::/0"]
            nat6 = bool(r.get("advertiseDefault")) and egress6
            advertise6 = ([entry_text(n, le=128) if n.prefixlen < 128 else str(n) for n in mine6]
                          if r.get("advertiseNetworkPrefixes", True) else []) + (["::/0"] if nat6 else [])
            plan["ipv6"] = {"link": str(link6), "prefixLength": 64, "ce": str(ce6),
                            "nodes": {n: str(addrs6[n]) for n in plan["nodes"]},
                            "accept": accept6, "deny": deny6, "advertise": advertise6, "denyDefault": True,
                            "advertiseDefault": nat6,
                            # what a node admits as source from the CE: the accepted prefixes and the link
                            "sources": [str(n) for n in sorted(collapse(nets6 + [link6], 6),
                                                               key=lambda n: (int(n.network_address), n.prefixlen))]}
            hashed += [deny6, accept6, advertise6, nat6]
        policy = hashlib.sha1(json.dumps(hashed, sort_keys=True).encode()).hexdigest()[:12]
        circuits.append(plan)
        status.append({"circuit": key, "network": net_key, "rev": plan["rev"], "policy": policy, "vlan": vlan,
                       "vrf": plan["vrf"], "device": device, "link": str(link),
                       "nodes": sorted(plan["nodes"].keys())})
        if accept6:
            status[-1]["families"] = [4, 6]
        for n, a in plan["nodes"].items():
            nodes[n]["circuits"].append({"circuit": key, "device": device, "parent": parent, "vlan": vlan,
                                         "mtu": plan["mtu"], "vrf": plan["vrf"], "table": plan["table"],
                                         "zone": zone, "address": f"{a}/{link.prefixlen}", "link": str(link),
                                         "nat": nat})
            if accept6:
                nodes[n]["circuits"][-1].update({"address6": f"{plan['ipv6']['nodes'][n]}/64", "link6": str(link6),
                                                 "sources6": plan["ipv6"]["sources"]})
    for n in nodes.values():
        n.update({"vlanPool": [int(vp.get("start", 1)), int(vp.get("end", 0))],
                  "parents": sorted(parents | {str(b.get("interface")) for p in (at.get("providerEdges") or {}).values()
                                               for b in ((p or {}).get("bearers") or {}).values()
                                               if (b or {}).get("interface")}),
                  "platformSources": sources})
    out = {"errors": errors, "circuits": circuits, "skipped": skipped, "nodes": nodes, "status": status,
           "vrfs": root_vrfs(req, circuits)}
    if platform:
        # (only with platform circuits: a plan without them is the same answer as before)
        out["platform"] = platform
    return out


def root_vrfs(req, circuits):
    """Per node, the networks with a root VRF in vpnTarget order: the node's egress entry (egress-plan nodes) for a
    network with egress, plus the network's circuits on the node (a circuit-only network gets a bare entry); a
    circuit with IPv6 carries its ipv6 plan and the node's address6 (<address>/64)."""
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
                                             "table": c["table"], "nat": "", "egress": False,
                                             "rd": f"{asn}:{c['vpnTarget']}" if 0 < asn <= 65535 else "",
                                             "circuits": []})
            v["circuits"].append(dict({k: x for k, x in c.items() if k != "nodes"}, address=c["nodes"][name]))
            if "ipv6" in c:
                v["circuits"][-1]["address6"] = f"{c['ipv6']['nodes'][name]}/64"
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
    nodes [{fabric, node, netns: {name: [vrf]}, rootDevices: [{name, master, addrs6}] (the root's VLAN devices in root
    VRFs or on a bearer interface within the attachment VLAN pool, with their global IPv6 addresses), rootBfd: [{peer,
    vrf, interface, status}] (the root bfdd's sessions), rootAttach6 (the circuits' IPv6 filter table is loaded),
    rootInputFilters {ipv4, ipv6} (the root VRFs' input filters ip shc_root_in, ip6 shc_root_in6 are loaded; absent in
    a node read from before them: not judged)}]. The input filters are judged against the root's VRFs on the node and
    the fabric's IPv6 core (status.realization.ipv6Core): a table without root VRFs (or the IPv6 one without the IPv6
    core) is left over; root VRFs without the table are reported as unprotected.
    A circuit has an IPv6 session when its realization entry lists family 6. Returns {findings: [str], counts}."""
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
                # a service prefix's owner is its network and the prefix (HybridNetwork/<ns>/<name>/<prefix>)
                if kind == "servicePrefix" and "/".join(owner.split("/")[:3]) in owners:
                    continue
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
        st = p.get("status") or {}
        # status.prefixes are the allocated prefixes; a provider-assigned subnet is only in status.subnets, and the
        # ledger records it for the placement as external
        mine = list(dict.fromkeys((st.get("prefixes") or []) + [
            s.get("prefix") for s in st.get("subnets") or [] if s.get("source") == "Provider" and s.get("prefix")]))
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
                svc = real.get("service") or {}
                svc_att = [c for c in svc.get("circuits") or [] if n["node"] in (c.get("nodes") or [])]
                if svc_att:
                    want |= {f"e{x.get('vpnTarget')}" for x in svc.get("networks") or []}
                for v in vrfs:
                    if re.fullmatch(r"e\d+", v) and v not in want:
                        findings.append(f"node {n['node']}: root VRF {v} belongs to no network with internet egress, "
                                        "attachment circuit or cluster the platform reaches")
                devs = {a.get("device") for a in att} | {c.get("device") for c in svc_att if c.get("device")}
                # the circuits on this node with an IPv6 session (families 4 and 6): their devices and VRFs
                att6 = [a for a in att if 6 in (a.get("families") or [4])]
                devs6, vrfs6 = {a.get("device") for a in att6}, {a.get("vrf") for a in att6}
                for d in n.get("rootDevices") or []:
                    if d.get("name") not in devs:
                        findings.append(f"node {n['node']}: circuit device {d.get('name')} (VRF {d.get('master') or 'none'}) "
                                        "belongs to no attachment circuit")
                    elif d.get("addrs6") and d.get("name") not in devs6:
                        findings.append(f"node {n['node']}: circuit device {d.get('name')} holds IPv6 address(es) "
                                        f"{', '.join(d['addrs6'])}, but its circuit has no IPv6 session")
                rin = n.get("rootInputFilters")
                if isinstance(rin, dict) and rin:
                    have_vrfs = bool(vrfs)
                    core6 = bool(real.get("ipv6Core"))
                    for fam, name, table, want in (("ipv4", "IPv4", "ip shc_root_in", have_vrfs),
                                                   ("ipv6", "IPv6", "ip6 shc_root_in6", have_vrfs and core6)):
                        if rin.get(fam) and not want:
                            findings.append(f"node {n['node']}: the root VRFs' {name} input filter ({table}) is "
                                            "loaded, but " + ("the root has no VRF" if not have_vrfs
                                                              else "the fabric has no IPv6 core"))
                        elif want and not rin.get(fam):
                            findings.append(f"node {n['node']}: the root has VRFs ({', '.join(sorted(vrfs))}), but their "
                                            f"{name} input filter ({table}) is not loaded")
                if n.get("rootAttach6") and not att6:
                    findings.append(f"node {n['node']}: the attachment circuits' IPv6 filter table (ip6 shc_attach6) is "
                                    "loaded, but no circuit on the node has an IPv6 session")
                # a BFD session in a VRF the kernel no longer has outlived its circuit (bgpd only deregisters it while
                # the VRF exists); it stays in bfdd until bfdd restarts
                for b in n.get("rootBfd") or []:
                    if (b.get("vrf") or "default") != "default" and b.get("vrf") not in vrfs:
                        findings.append(f"node {n['node']}: BFD session to {b.get('peer')} in VRF {b.get('vrf')} "
                                        f"({b.get('interface') or 'no interface'}, {b.get('status') or 'unknown'}) "
                                        "outlives its VRF")
                    elif ":" in str(b.get("peer") or "") and b.get("vrf") not in vrfs6:
                        # an IPv6 peer in a VRF none of whose circuits on this node runs an IPv6 session (any more)
                        findings.append(f"node {n['node']}: BFD session to {b.get('peer')} in VRF {b.get('vrf')} "
                                        f"({b.get('interface') or 'no interface'}, {b.get('status') or 'unknown'}) "
                                        "belongs to no IPv6 circuit session")
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
           "circuit-plan": circuit_plan, "check-circuit": check_circuit, "attachments-plan": attachments_plan,
           "reserved-ranges": reserved_ranges_action, "service-plan": service_plan,
           "service-prefix-check": service_prefix_check, "check-cluster-networks": check_cluster_networks}


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

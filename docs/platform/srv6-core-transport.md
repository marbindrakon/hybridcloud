# SRv6 core transport

For the platform team. On every border gateway node the fabric root and one namespace per gateway are provider
edges of a small core of their own: a node-local core bridge joins them, and the networks' VPN routes travel between
them as L3VPN routes. This page describes the realization option that selects how that core carries them: MPLS (the
default) or SRv6. Nothing a tenant writes changes: the objects, the route targets, the site side (EVPN-VXLAN,
WireGuard, the cloud routers) and the routes the sites see stay the same. The switch lives in the EDA vars
`common/vars/fabric_realization.yml` (the generic defaults, shipped on MPLS) and in the installation's
`FABRIC_REALIZATION` (the aap-config environment, JSON; per fabric under `fabrics.<fabric name>`). Installation values
never go into Git, the CRDs or the forms ([Lab configuration policy](../lab-config.md)).

A fabric has one core transport: every border gateway node, its root and all its gateways use it. Both transports
are supported; `mpls` is the default.

## What the switch changes

| | `mpls` (default) | `srv6` |
|---|---|---|
| IS-IS on the core | segment routing: a prefix SID per loopback, MPLS labels | dual-stack IS-IS as before, without segment routing; each provider edge's locator is a prefix of its passive `lo`, nothing is redistributed |
| VPN sessions to the root | over the IPv4 loopbacks | over the IPv6 loopbacks, with the extended next hop capability; both VPN families on them |
| Per VRF | an MPLS label (`label vpn export auto`) | one SID (`sid vpn per-vrf export auto`) that carries IPv4 and IPv6 (`End.DT46`) |
| Kernel | MPLS input on the core legs and `lo` | SRv6 enabled on the core leg, a routing rule and a guard for the SID block, no MPLS |
| Per packet on the core | the label stack | an outer IPv6 header and a routing header: 64 bytes |

The IPv4 loopbacks stay as the router IDs. Only what rides the core changes; the VRFs, their tables and the route
targets are the same on both.

## The keys

```yaml
core:
  loopbackPool6: ""       # the IPv6 loopbacks (dual-stack core); srv6 needs it
  transport: mpls         # mpls or srv6: the fabric's core transport
  srv6:
    block: ""             # the SID block: a /32 inside 5f00::/16; srv6 needs it
```
- **`core.transport`:** `mpls` or `srv6`, for the whole fabric. Empty counts as `mpls`. There is no per-node
  transport.
- **`core.srv6.block`:** the SID block, a /32 inside `5f00::/16`, the range RFC 9602 sets aside for SRv6 SIDs. Draw
  one at random for the installation, so two installations that ever meet don't collide. It stays inside the core:
  never announce it. The network job treats the block as an exclusive range whenever it is set, as if it were listed
  in `network.exclusiveRanges`: a tenant prefix that overlaps it is refused (`PrefixReserved`), because the routing
  rule below would take such a prefix out of its network's VRF. A block can be set while the transport is `mpls`; it
  is checked as soon as it is set.

The HybridFabric job refuses the fabric (`InvalidSpec`) when:
- the transport is neither `mpls` nor `srv6`, or the realization sets `core.transportByNode` (not supported);
- the transport is `srv6` while `core.loopbackPool6` or `core.srv6.block` is empty;
- the block is not exactly a /32 inside `5f00::/16`, or overlaps `core.loopbackPool6`, `core.icmpSource6`,
  `attachments.linkPool6` or `egress.ipv6.routedPrefixes`;
- with a block, the fabric has more than 255 nodes or the highest provider-edge index is above 255 (a
  `core.loopbackPool` or `core.linkSubnet` of at most 256 addresses keeps it at 254; the defaults do).

On `mpls` and with no block, nothing of this applies and every node renders and applies as before.

## SID layout

The block is split as micro-SIDs (uSID, F3216): 32 bits of block, 16 bits of node, 16 bits of function. Each
provider edge has a locator, the /48 whose node field is

    node id = node position * 256 + provider-edge index

with the node position the 1-based place of the node in `borderGateway.nodes` and the index the one the
provider edge already has (the root is 1, the gateways 2 and up). Example with the made-up block `5f00:db8::/32`:

| Provider edge | Node position | Index | Node id | Locator |
|---|---|---|---|---|
| root on the first node | 1 | 1 | `0x101` | `5f00:db8:101::/48` |
| gateway with index 2 on the first node | 1 | 2 | `0x102` | `5f00:db8:102::/48` |
| root on the second node | 2 | 1 | `0x201` | `5f00:db8:201::/48` |
| gateway with index 2 on the second node | 2 | 2 | `0x202` | `5f00:db8:202::/48` |

The loopbacks repeat on every node (each node is its own core); the locators do not, so a SID seen in a capture or a
route table names one provider edge on one node. FRR allocates the function of each VRF's SID from the locator.
`shc_fabric.py pe-addresses` returns `locator` and `locatorNode` (the node id) next to the loopbacks when it is given
the node position and the realization has a block.

The block is also a platform range for attachment circuits: an IPv6 accept entry inside it is refused, and every
IPv6 circuit's deny list holds it, so a customer edge can never announce a route into it. The IPv6 egress filter's
default `denyDestinations6` holds `5f00::/16`.

## What a node needs

- **The dual-stack core.** The VPN sessions and the encapsulation source are the IPv6 loopbacks, so
  `core.loopbackPool6` must be rolled out first ([IPv6 realization, Transport](ipv6-realization.md#transport-dual-stack-core)).
- **FRR 8.5 or later.** `behavior usid` and `sid vpn per-vrf export` arrive in 8.5. FRR 8.x and 10.x write the
  locator back in different text forms and frr-reload compares text, so the jobs read the major version from the
  installed package and render the form that version prints.
- **A kernel with SRv6** (`End.DT46`, the `seg6` encapsulation), as the el9 kernel has.
- **No MTU change.** The core legs run at the fabric's `overlayMtu` + 100 bytes, so a VRF packet of `overlayMtu`
  bytes still fits with the 64 bytes of encapsulation.
- **The root namespace prepared by the node's own setup.** The root instance runs in the host's namespace, and the
  apply does not set its forwarding: the host must forward IPv4 and IPv6. No host interface outside the VRFs may
  take routes from router advertisements (a host that routes IPv6 ignores them, and IS-IS adds IPv6 routes to the
  main table): on `srv6` the apply fails while one does, where on `mpls` it leaves the IPv6 core out. The host's
  IPv4 reverse-path filtering for `all` interfaces (`net.ipv4.conf.all.rp_filter`) must be 0: the apply never
  writes the host's `all` or `default`, and on `srv6` it fails, before changing anything, while `all` is not 0
  (why, and what it sets on its own devices, is below). What the host must not have on either transport (an IPv4
  policy rule of its own in front of the local table, a VRF of its own on a table the fabric uses) is in
  [Border gateway nodes](border-gateway-nodes.md#a-networks-vrf-and-the-nodes-own-addresses). The gateway
  namespaces are the apply's own and need none of this.

## The routing rule and the kernel guard

On `srv6`, in the root namespace and in every gateway namespace of a node, the apply sets:

- **The locator on `lo`.** IS-IS announces the locator because `lo`, a passive IS-IS interface, holds an address
  with the locator's length: the anchor, the locator's first address (`<locator>::1/48`; its function field is 0 and
  FRR allocates SID functions from 1, so it is never a SID), added `nodad noprefixroute`. A passive interface's
  prefixes are announced on every start of isisd, so a restart of FRR's daemons (watchfrr restarts all of them when
  zebra dies), a unit restart, a reload or a boot all leave the locator announced. A redistributed discard route did
  not: after watchfrr's restart the redistribution was in the running configuration but was not applied, and the
  provider edge's SIDs were unreachable from every other node until a reboot. Without the kernel's prefix route for
  the anchor (`noprefixroute`) the apply adds the locator as a `blackhole` in the main table at metric 4278198272,
  so a packet for an unallocated SID is dropped without an answer (with the prefix route the node would answer it
  with an unreachable); the SIDs are longer routes and win. The anchor itself is a local address of the node (the
  core can reach it like a loopback; the guard drops it from anywhere else, as all of the block); the kernel also
  adds the locator's subnet-router anycast address (`<locator>::`). The apply remembers the anchor in
  `<instance>.locator6` next to the JSON and removes it when the locator changes, on the root's `down`, and on a
  run with `mpls` that finds the file.

- `net.vrf.strict_mode=1` (the decapsulating SIDs need it; the apply fails, naming them, while two VRFs of the
  namespace share a table), `net.ipv6.seg6_flowlabel=1` (the outer flow label follows the inner flow) and the
  encapsulation source (`ip sr tunsrc`) to the namespace's own IPv6 loopback. No MPLS setting.
- **No routing header accepted.** `seg6_enabled` is 0 on `all` and `default` (so every device created later inherits
  0) and on the core leg, and at the end of each run the apply sets 0 on every device of the instance an earlier
  configuration left at 1: in a gateway namespace every device; in the root `lo`, the core bridge, `egress0`, the
  egress VRF, the network VRFs `e<n>`, their veths `xe<n>` and `xo<n>` and the circuits' devices, never the
  management interface. The SIDs that decapsulate are routes, not local addresses, and do not read the setting; with
  it on, a packet addressed to any local address of the provider edge could carry a segment list on to any SID,
  past the guard below.
- **The unreachable route** for the block in the namespace's main table, at the highest metric (4278198272). Every
  real route wins over it (the locators are longer prefixes). It is there for a SID whose locator has gone from
  IS-IS while a VPN route still names it: the lookup through the rule below ends at this route instead of following
  a default route the host's main table may hold.
- **The routing rule** `ip -6 rule … to <block> lookup main pref 999`. After encapsulating a packet that came in on a
  VRF device, the kernel looks the outer destination (the SID) up in that VRF, where the VRF's default route (the
  network's own, or the `unreachable` default) takes it and nothing reaches the core. The rule sends every lookup for
  the block to the main table, where IS-IS holds the locators.
- **The guard**, nftables table `inet shc_core_guard`: in raw prerouting, a packet whose IPv6 destination lies in
  the block and that did not arrive on the core leg or `lo` is counted and dropped. The rule above makes the block
  routable from every VRF; without the guard a host in any network could address another provider edge's SID
  directly and have its packet decapsulated into that SID's VRF. Packets the node encapsulates itself never pass
  prerouting, so the guard does not touch them.
- **ICMPv6 errors from the VRF's own source.** A packet a SID decapsulates into a VRF came in by the core leg, which
  is outside every VRF. When the node sends an ICMPv6 error for it (Packet Too Big toward a site's smaller leg, Time
  Exceeded, Unreachable), the kernel picks the error's source from the device the packet came in by, so among the
  default VRF's addresses: the IPv6 loopback, a core address that is never announced outside the core. (With MPLS the
  popped packet comes in by the VRF device, and the error carries the VRF's ICMPv6 source.) Such an error is dropped
  by the root's egress filter on its way to the internet, which stalls path MTU discovery toward a site behind a small
  leg, and shows a core address to tenants. With an ICMPv6 source (`core.icmpSource6`), the guard table therefore has
  a second chain, on the output hook: an ICMPv6 error that leaves by the core leg toward an address outside the core
  (the block, `core.loopbackPool6`, link-local) gets the namespace's ICMPv6 source, the address every VRF device of
  the namespace holds. The error is generated in the VRF and passes the output hook as itself once, before it is
  encapsulated. The chain is a filter chain, not a route chain: a changed source must not make the kernel route the
  packet again, outside its VRF. Errors toward core addresses keep the loopback; the default VRF routes nothing else
  into the core leg.
- **No reverse-path filtering on the root's network VRFs.** Each network VRF `e<n>` in the root gets IPv4
  `rp_filter=0`. A SID decapsulates IPv4 into the VRF with the VRF device as input, and the route back to its source
  in that VRF points at the core bridge, outside the VRF: both strict and loose filtering drop such a packet. The
  kernel applies the larger of `all` and the device's value, so the device's 0 holds only while the host's `all` is
  0, which is why the apply requires it. The attachment circuits' packets are checked on their own devices, which
  stay strict (1).
- **A VRF's IPv4 lookup stays out of the default VRF.** With reverse-path filtering off on a network VRF, nothing
  but the rule order keeps a decapsulated IPv4 packet addressed to one of the node's default-VRF addresses from being
  delivered to the node. On both transports the apply puts the IPv4 local-table rule behind the VRF rule and gives
  every VRF table an IPv4 unreachable default
  ([Border gateway nodes](border-gateway-nodes.md#a-networks-vrf-and-the-nodes-own-addresses)).

The apply loads the guard first, before the other SRv6 settings and before FRR can hold a SID, and any of these
steps that fails fails the apply.

**The guard between jobs.** On `srv6` the jobs install the templated units `shc-core-guard@<instance>.service` and
`.timer` and enable one timer per instance: `shc-core-guard@root.timer` for the root and `shc-core-guard@gw-<name>.timer`
for each gateway, as the IPv6 egress filter has its own timer. Every 15 seconds (the first time 30 seconds after
boot) the service runs `shc-fabric-apply <instance> guard`, which compares the instance's guard table with what the
last apply loaded and loads it again when it is missing or differs (a ruleset flush, a hand change). It prints
`guard=ok`, `guard=restored`, or `guard=none` (an instance on `mpls`, or a gateway whose namespace is gone). A
gateway's teardown stops its timer; the fabric's teardown stops every remaining one and removes the units. Nothing of
it is installed on `mpls`.

Packets take other netfilter hooks than on MPLS: a decapsulated packet passes prerouting (with the VRF device as
input) and then forward, never output; a packet about to be encapsulated passes prerouting only. The node's own
filters on the networks' traffic (the attachment circuits' chains, the connection tracking zone of a network's
traffic) keep their effect on `srv6`; the rules that needs are added only when the transport is `srv6`. The TCP MSS
clamps sit on the hook each transport uses
([Border gateway nodes](border-gateway-nodes.md#tcp-mss-clamps)).

## Choosing and changing the transport

Choose the transport when you set up the fabric's realization; for `srv6`, the dual-stack core (`core.loopbackPool6`)
and the block first, then `core.transport: srv6`. The realization does not switch a running node in place. To change
the transport of an existing environment, change `core.transport`, re-apply the nodes (run the HybridFabric and then
each CloudGateway, Retry now) and restart their FRR instances, or reboot the nodes. The jobs themselves reload FRR,
they don't restart it: a node that ran `mpls` and is applied with `srv6` for the first time gets a reload from the
job, and the restart (or the reboot) is yours.

On `srv6` both address families ride the IPv6 core, so a step of the IPv6 core that fails fails the apply and the
job. On `mpls` the same failure is a warning and IPv4 carries on.

## Readiness

After each node the jobs wait for the node to be ready on the transport before they go on. On `srv6`:
- **Both instances:** strict VRF mode; `seg6_enabled` 0 on `all`, `default` and the core leg (no routing header
  accepted); the rule to the block at preference 999; the IPv4 local-table rule behind the VRF rule, as on `mpls`; the guard table loaded with its drop rule, a rule that drops
  what is addressed to the block unless it came in on the core leg or `lo`; the instance's own locator announced: its anchor on `lo`, the connected route zebra
  makes of it, and its prefix in the instance's own IS-IS LSP (the state a lost announcement leaves: sessions up,
  routes in, the locator missing from the own LSP and so from every other node); and for every VRF whose configuration exports a per-VRF SID a
  `seg6local … End.DT46 … vrftable <table>` route in the kernel (the VRF's SID). Readiness does not look at the
  unreachable route for the block.
- **The root** also requires that the provider-edge group listens on `core.loopbackPool6`. Its gateways' sessions it
  reports and does not require: a gateway whose session is up but lacks VPNv4 or VPNv6, or whose IPv6 loopback or
  locator is not installed from IS-IS, is listed in the node record's `pendingPeers` and in its message ("not ready
  on the SRv6 core: …"), and the node's `ipv6Vpn` reads `Partial` (some gateways complete) or `NoNeg` (none);
  `Established` when every gateway is complete, `NoPeers` without a gateway session. The fabric does not fail for it:
  the gateway's own job repairs it, and a failed fabric would hold that job back.
- **A gateway** also checks IPv6 forwarding in its namespace, its IS-IS adjacency with the root, its session to the
  route reflector with VPNv4 and VPNv6 negotiated over the IPv6 loopbacks, and the root's IPv6 loopback and locator
  installed from IS-IS.

The MPLS input and VPN label checks run on `mpls` only. The removal of a VRF a network no longer needs, the
teardown and the orphan check's view of a node are described in [Border gateway nodes](border-gateway-nodes.md).

## What a tenant and an operator see

Paths through an `srv6` core look different from MPLS in the tools. These were seen in a test setup with the same
kernel and FRR versions, not yet with this realization:
- A traceroute from a site shows the egress provider edge, answering from its address in the network's VRF, and no
  core address; the ingress provider edge does not appear. IPv6 traceroutes may show the provider edges' hops
  silent.
- A gateway that decapsulates a packet too large for its site leg answers with Packet Too Big, sourced from its core
  IPv6 loopback, not from the VRF's ICMPv6 source: that one core address can reach a tenant.
- The outer packets on the core legs carry the SID (block, node id, function) as destination and the sending
  namespace's IPv6 loopback as source; a capture on the core bridge names provider edge and node by the node id.

## Status

| Where | Field | Content |
|---|---|---|
| HybridFabric and CloudGateway `status.realization` | `coreTransport` | `mpls` or `srv6`, on every fabric |
| HybridFabric `status.realization` (`srv6` only) | `srv6Locators` | `{<node>: <the root's locator>}` |
| CloudGateway `status.realization` (`srv6` only) | `srv6Locators` | `{<node>: <the gateway's locator>}` |
| HybridFabric and CloudGateway `status.nodes[]` (`srv6` only) | `transport`, `locator` | `srv6` and the node's locator of that root or gateway |
| HybridFabric `status.nodes[]` (`srv6` only) | `pendingPeers` | the IPv6 loopbacks of the gateways whose session is up but not complete on the SRv6 core (see Readiness); empty when none |

On `mpls` the `srv6Locators` field and the node records' `transport`, `locator` and `pendingPeers` are absent.

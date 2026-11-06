# SRv6 core transport

For the platform team. On every border gateway node the fabric root and one namespace per gateway are provider
edges of a small core of their own: a node-local core bridge joins them, and the networks' VPN routes travel between
them as L3VPN routes. This page describes the realization option that selects how that core carries them: MPLS (the
default) or SRv6. Nothing a tenant writes changes: the objects, the route targets, the site side (EVPN-VXLAN,
WireGuard, the cloud routers) and the routes the sites see stay the same. The switch lives in the EDA vars
`common/vars/fabric_realization.yml` (the generic defaults, shipped on MPLS) and in the installation's
`FABRIC_REALIZATION` (the aap-config environment, JSON; per fabric under `fabrics.<fabric name>`). Installation values
never go into Git, the CRDs or the forms ([Lab configuration policy](../lab-config.md)).

The transport is chosen per node, and every namespace on a node (the root and all its gateways) uses the node's. A
node's core never reaches another node, so nodes on different transports work side by side: you can move one node,
watch it, and move the next.

## What the switch changes

| | `mpls` (default) | `srv6` |
|---|---|---|
| IS-IS on the core | segment routing: a prefix SID per loopback, MPLS labels | dual-stack IS-IS as before, without segment routing; each provider edge's locator enters level 2 as a discard route, restricted to the locator |
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
  transport: mpls         # mpls or srv6: the transport of every node not listed below
  transportByNode: {}     # <node>: mpls or srv6; overrides transport for that node
  srv6:
    block: ""             # the SID block: a /32 inside 5f00::/16; srv6 needs it
```
- **`core.transport`:** `mpls` or `srv6`. Empty counts as `mpls`.
- **`core.transportByNode`:** keys are names from the HybridFabric's `borderGateway.nodes`, values `mpls` or `srv6`.
  A node not listed takes `core.transport`. `shc_fabric.py node-transport` (`{core, node}` in, `{transport}` out)
  resolves one node the same way the jobs do.
- **`core.srv6.block`:** the SID block, a /32 inside `5f00::/16`, the range RFC 9602 sets aside for SRv6 SIDs. Draw
  one at random for the installation, so two installations that ever meet don't collide. It stays inside the core:
  never announce it, and list it in `network.exclusiveRanges` so no tenant prefix overlaps it (the routing rule below
  would take such a prefix out of its network's VRF). A block can be set while every node is on `mpls`; it is
  checked as soon as it is set.

The HybridFabric job refuses the fabric (`InvalidSpec`) when:
- a transport is neither `mpls` nor `srv6`, or a `transportByNode` key names no node of the fabric;
- a node resolves to `srv6` while `core.loopbackPool6` or `core.srv6.block` is empty;
- the block is not exactly a /32 inside `5f00::/16`, or overlaps `core.loopbackPool6`, `core.icmpSource6`,
  `attachments.linkPool6` or `egress.ipv6.routedPrefixes`;
- with a block, the fabric has more than 255 nodes or the highest provider-edge index is above 255 (a
  `core.loopbackPool` or `core.linkSubnet` of at most 256 addresses keeps it at 254; the defaults do).

With every node on `mpls` and no block, nothing of this applies and every node renders and applies as before.

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

## The routing rule and the kernel guard

In the root namespace and in every gateway namespace of an `srv6` node the apply sets:

- `net.vrf.strict_mode=1`, SRv6 processing (`seg6_enabled`) on `all`, `default` and the core leg,
  `net.ipv6.seg6_flowlabel=1` (the outer flow label follows the inner flow) and the encapsulation source
  (`ip sr tunsrc`) to the namespace's own IPv6 loopback. No MPLS setting, and MPLS input is switched off where it was
  on.
- **The routing rule** `ip -6 rule … to <block> lookup main pref 999`. After encapsulating a packet that came in on a
  VRF device, the kernel looks the outer destination (the SID) up in that VRF, where the VRF's default route (the
  network's own, or the `unreachable` default) takes it and nothing reaches the core. The rule sends every lookup for
  the block to the main table, where IS-IS holds the locators.
- **The guard**, nftables table `inet shc_core_guard`: in raw prerouting, a packet addressed into the block that did
  not arrive on the core leg or `lo` is counted and dropped. The rule above makes the block routable from every VRF;
  without the guard a host in any network could address another provider edge's SID directly and have its packet
  decapsulated into that SID's VRF. Packets the node encapsulates itself never pass prerouting, so the guard does
  not touch them.

The guard is loaded before FRR can hold a SID, and a step that fails fails the apply, so a node never holds a SID
without its guard (fail closed). Back on `mpls`, the rule, the guard and the encapsulation source are removed.

Packets take other netfilter hooks than on MPLS: a decapsulated packet passes prerouting (with the VRF device as
input) and then forward, never output; a packet about to be encapsulated passes prerouting only. The node's own
filters on the networks' traffic (the TCP MSS clamp on the egress veths, the attachment circuits' chains) keep their
effect on an `srv6` node; the rules that needs are added only when the node's transport is `srv6`.

## Moving a node and back

1. Roll out the dual-stack core (`core.loopbackPool6`) if the fabric doesn't have it.
2. Draw the block and set `core.srv6.block` (and add it to `network.exclusiveRanges`). This changes nothing on the
   nodes.
3. Set `core.transportByNode.<node>: srv6` for one node and run the HybridFabric (Retry now). The job restarts that
   node's FRR instances (a transport change is not reloaded), so the node's VPN sessions go down once; the other
   nodes carry the sites meanwhile, as in any node-by-node run. The node's gateways follow without a separate Retry
   now on each CloudGateway.
4. Check the node (below), then move the next one the same way. When every node is on `srv6`, you can set
   `core.transport: srv6` and empty `core.transportByNode`; nothing changes on the nodes.

Back is the same with `mpls`: set the node's entry to `mpls` (or remove it while `core.transport` is `mpls`) and run
the HybridFabric. The block can stay set.

## Readiness

After each node the jobs wait for the node to be ready on its transport before they go on. On `srv6`, per namespace:
the settings and the rule above, the guard table, IS-IS adjacencies, both VPN families Established over the IPv6
loopbacks, and for every VRF a `seg6local … End.DT46 … vrftable <table>` route (the VRF's SID). The MPLS input and
VPN label checks run on `mpls` nodes only. The HybridFabric's `status.realization` names each node's transport.

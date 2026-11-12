# Border gateway nodes

For the platform team. What the fabric and gateway jobs do on a border gateway node whatever the fabric's core
transport: how a network's VRFs are kept apart from the node's own addresses, what the host must not have, the TCP
MSS clamps, what a network that leaves the node leaves behind, what the health check reads and reports, what the
orphan check reports, and what a teardown removes. The transport itself (MPLS or
SRv6) is on its own page ([SRv6 core transport](srv6-core-transport.md)); the dual-stack core and the IPv6 egress
are in [IPv6 realization](ipv6-realization.md).

On every node the fabric root runs in the host's namespace (the route reflector and the provider edge of the egress
and circuit VRFs) and each gateway in a namespace of its own, `gw-<gateway>`. The apply (`shc-fabric-apply`) builds
each instance's kernel side from a file the job writes; FRR runs in each instance.

## A network's VRF and the node's own addresses

A packet that enters a network's VRF (from its site, from a circuit, or from the core) must stay in that VRF: it may
reach the VRF's own addresses and routes, never the node's addresses in the default VRF (its loopbacks, its core
links, a gateway's site underlay address) or the host's main table. In the root namespace and in every gateway
namespace the apply therefore:

- moves the kernel's IPv4 rule for the local table behind the VRF rule: `lookup local` at preference 32765 instead
  of 0 (the new rule is added before the old one goes). In front of the VRF rule, a packet in a VRF addressed to one
  of the default VRF's addresses would be delivered to the node; behind it, a lookup that starts in a VRF ends in
  that VRF's table, where the VRF's own addresses are local as before. The IPv6 rules keep the kernel's order.
  Removing the root instance puts the kernel's order back in the host's namespace.
- gives every VRF table an IPv4 `unreachable` default at the highest metric (4278198272), next to the IPv6 one the
  dual-stack core already gives it. Every real route wins over it; a destination the VRF has no route for is
  answered unreachable inside the VRF instead of falling through to the main table, where the host's default route
  would take it.

Readiness checks the rule order on both transports: the fabric and gateway jobs wait until the IPv4 `lookup local`
rule lies behind the VRF rule in the namespace.

**What the host must not have.** The root instance runs in the host's namespace, so the apply refuses, and fails
before it changes anything, while:
- the host has an IPv4 policy rule of its own between preferences 0 and 32765 (the kernel's VRF rule aside): moved
  behind it, the local table would no longer come first for the host's own traffic that matches it;
- a VRF that is not the instance's holds a routing table the configuration gives to one of the instance's VRFs (the
  egress VRF, a network's or a circuit's VRF; in a gateway namespace, a network's VNI): the VRF the run creates would
  share that table.
- with the service VRF on the node: the host has a device whose name starts with `so` or `se` that is not one of the
  service's veth pairs (`so<n>`/`se<n>`) and not a VRF: the service VRF's filter names its links by those patterns and
  would drop that device's traffic. `check-fabric` refuses the same for the interfaces the realization names
  (`nodes.trunkInterface`, `core.bridge`, `egress.parentInterface`, the bearers' `interface`) while `service.enabled`.

The first two checks run in a gateway namespace that exists already as well. A refusal prints `shc-fabric-apply: <reason>`
and exits with status 4; the job's failure and the object's status message carry that line. What only the SRv6 core
refuses is printed as `transport srv6: <reason>` ([SRv6 core transport](srv6-core-transport.md#what-a-node-needs)).

## What a network reaches of the node itself

Some addresses of the node do sit inside a network's VRFs, and FRR listens in every VRF that has a BGP instance (BGP
on TCP 179, BFD on UDP 3784, 3785 and 4784): a gateway VRF holds the node's ICMPv6 source (`core.icmpSource6`); a
root VRF `e<n>` holds the node's ICMPv6 source, the node's end of the network's egress veth and its circuits'
addresses; the egress VRF holds the hand-off address. A network's hosts reach these through the VRF: at a gateway
from the site, at the root from the core, and into the egress VRF through the network's egress veth. No session of
the node runs there with a tenant host, so the node answers such traffic like a host with no open port:

- **Gateway namespaces**, with the dual-stack core: table `ip6 shc_gw_in6`, input hook, every VRF device of the
  namespace. ICMPv6 passes (echo, errors, neighbour discovery); TCP segments with ACK or RST pass (the replies to
  connections the node opened; any other such segment is answered as a closed port would); every other TCP segment
  (a connection request, FIN, NULL or XMAS probes) gets a reset, so a port where FRR listens looks closed; UDP gets
  a port unreachable; every other protocol, and a fragment without its transport header, is dropped. It is loaded
  before the VRF devices take the ICMPv6 source and stays whether or not the node announces that address to the site
  (`gateway.announceIcmpSource6`, [IPv6 realization](ipv6-realization.md#transport-dual-stack-core)).
- **The root**, for a packet delivered to the node in the egress VRF or a network's VRF that did not arrive on one
  of that VRF's own legs: a network's egress veth (its own stateful filter admits only replies and ICMPv6 errors
  from the internet) or a circuit's device (its own filter admits BGP from the circuit's link, BFD and ICMP from the
  customer edge router). What arrives on the hand-off device (the internet, the other node's networks) is filtered
  too; only the upstream routers' BGP sessions of the IPv6 egress are admitted there. Table `ip shc_root_in`
  (always, while the root has VRFs): ICMP and TCP segments with ACK or RST pass, everything else is dropped without
  an answer. Table `ip6 shc_root_in6` (with the dual-stack core): the upstream routers' BGP on the hand-off device
  and link-local sources from a network's egress veth (the sessions over the veths) pass, then the same answers as
  in a gateway namespace. A run that leaves the dual-stack core out because a host interface takes router
  advertisements keeps the IPv6 table as it was.

- **The edge routers of an AWS or Google Cloud site** (part of the site's gateway): table `inet shc_edge_in`, input
  hook, every network VRF of the router. From a VRF device only BGP with the cloud's own peers on their own leg
  (the transit gateway's Connect peers, the Cloud Router), ICMP, ICMPv6 and TCP segments with ACK or RST reach the
  router; everything else is dropped. The router's agent loads it on every run; a router takes it with the agent it
  was created with, so routers created before it carry none until they are replaced (a new gateway, or the edge
  helper's `replace`).

The filters are stateless: they keep no connection state ("a TCP segment with ACK or RST" stands in for
"established"; a stateful rule would make the namespace track and reassemble every network's forwarded traffic), and
forwarded traffic never meets them. Being stateless, they also refuse the UDP replies to requests the node itself
sends from such a VRF (a DNS query, NTP or `iperf -u` run on the node inside a VRF gets no answer) and drop TCP that
reaches the node fragmented. The answers are the ones a closed port gets: resets are not rate-limited, unreachables
only by the kernel's global ICMP limit; both are no larger than the probe plus 48 bytes and go only toward addresses
routed in that VRF. Each refusing rule counts its packets (`nft list table ip6 shc_gw_in6` in the gateway's
namespace, `nft list table ip shc_root_in` and `nft list table ip6 shc_root_in6` on the node). A table follows the
VRFs on every run and goes with the last of them (and an IPv6 table with the dual-stack core).

## The root's IPv4 ICMP errors toward a network

```yaml
core:
  icmpSource4: 192.0.0.8   # the IPv4 source of the root VRFs' ICMP errors; empty = the kernel's choice
```

A network's IPv4 traffic that crosses the root (toward a circuit, or out through the egress link) meets the root
twice: in the network's VRF and in the egress VRF. When it does not fit the next link or its TTL runs out there, the
root answers with "fragmentation needed" or "time exceeded". Without `core.icmpSource4` the kernel takes the
answer's source from the VRF's devices, which for these VRFs is an address of the egress link (`egress.linkPool`,
169.254.0.0/16 by default) or a circuit's, depending on the order the devices were created in; some sites drop such
sources (OpenShift Virtualization: OVN-Kubernetes reserves part of 169.254.0.0/17), and the guests behind them then
lose IPv4 path MTU discovery and see a silent hop.

With `core.icmpSource4` (default `192.0.0.8`, the IPv4 dummy address of RFC 7600, which the site gateways' kernels
already answer from) every root VRF device (the egress VRF and each network's and circuit's VRF) holds that address as
a /32 on every node. The kernel prefers it for every route whose next hop lies outside the VRF, which is every route
back to a network's hosts (over the core). The egress VRF's routes back to the networks (each network's source NAT
address, each platform prefix) name the node's hand-off address (`egress.nodes.<node>.address`) as their source:
the error crosses the network's VRF on its way, and a VRF drops an arriving packet whose source is one of its own
addresses, so it cannot be `icmpSource4`. The hand-off address must lie outside the ranges the sites reserve
(OVN-Kubernetes: its masquerade, join and transit subnets). Errors toward a circuit's customer edge router keep the
circuit's address (its own link), and errors toward the internet the egress link's or hand-off address as before.
The address is never announced and is local in every root VRF, so what a network sends to it meets the root's input
filter; a tenant prefix that overlaps it is refused (`PrefixReserved`). The HybridFabric refuses (`InvalidSpec`) a
value that is no single IPv4 address, is link-local or loopback, or lies inside `core.loopbackPool` or
`core.linkSubnet`. Source NAT is unchanged. Setting or changing it adds the address and replaces the egress VRF's
routes with their source, without an interruption. A node that drops such errors counts them as martian sources
(`in_martian_src` in `/proc/net/stat/rt_cache`, per CPU).

## TCP MSS clamps

The node clamps the MSS of TCP connection setups where a packet is about to leave on a link smaller than the path it
came from, one rule per direction:
- **Toward the internet** (IPv6 egress): a network's SYN leaving its VRF for the egress VRF (`oifname "xe*"`), at
  `egress.ipv6.wanMtu - 60`.
- **From the internet:** the reply arriving on the hand-off device, clamped to the same value in the egress VRF while
  it is forwarded to the network's veth (`oifname "xo*"`).
- **Toward a site** (each gateway): SYNs and SYN-ACKs leaving the VRF on the site's bridge (`oifname "br*"`), to the
  route's MTU, so a site behind a small leg (a WireGuard tunnel) gets segments that fit. On `mpls` the packet the core
  delivers is sent from the VRF device and passes the output hook; on `srv6` it is decapsulated on the input path and
  passes the forward hook: the rule sits on the hook the transport uses.

A site's own SYN on its way into the core is not clamped at the gateway; a segment that then exceeds the far site's
leg is answered with Packet Too Big or fragmentation needed from the gateway on the far side.

## A network that leaves the node

When a network is no longer placed at a site, or loses its egress and its circuits at the root, the apply deletes
its VRF device and the job reloads FRR without the network. In a gateway the network first leaves FRR: before the
apply runs, the job takes the VRF's layer 3 VNI and then its BGP instance out of the running instance (only for VRFs
the namespace has and the configuration no longer lists; nothing at all when none is removed), and waits 3 s. With
the devices deleted while FRR still held the VRF and the site's routes in it, FRR 8.5.3's zebra could crash
withdrawing those routes, and watchfrr's restart of the instance cut the site off for about half a minute. A reload removes the lines of the VRF's `vrf` stanza
but never the VRF itself, so FRR would keep it until its next restart (`show vrf`: `vrf <name> inactive
(configured)`). After the reload the job therefore removes, in the root and in each gateway, every VRF that FRR lists
as inactive, that the rendered configuration does not name and that has no BGP instance left. It touches no other VRF
and resets no session. A VRF that FRR refuses to remove is named in the job's output and stays: it has no device and
no routes, and the next job tries again.

## The health check

The job template `fabric-health` runs on a schedule (realization `health.intervalSeconds`, default 600 s, at least
300 s) and reads every node of every fabric once over SSH. It changes nothing on a node and takes no lease: while the
fabric's lease is held, or a fabric event is queued, it leaves the nodes alone (`status.health.skipped`), and a read
that a job overlapped is discarded. Every command on a node has its own time limit and the whole read has
`health.nodeReadTimeoutSeconds`.

What it reads, per instance (the root, and each gateway whose site is not paused):

- the units of the instance and whether they start at boot: the root's apply unit, FRR, the IPv6 egress guard's and
  the SRv6 core guard's timers; a gateway's instance unit and its core guard's timer; any other unit of the
  fabric that failed;
- the instance's start at boot: a failed apply, with the apply's own lines from the journal of that start, and a root
  started without its IPv6 side (a `warning:` line);
- the routing daemons (`show watchfrr`) and their crashes in the last `health.daemonRestartWindowMinutes`
  (systemd-coredump's journal entries; a gateway's by its pathspace);
- drift: the state the apply manages now (`shc-fabric-apply <instance> snapshot`) against the record a successful
  apply leaves (`/etc/shc-fabric/<instance>.applied`), and the FRR files against the record of what FRR last loaded.
  A node without the records (applied by an earlier release and not since) is not judged for drift;
- what FRR's running configuration still lacks of its file, as the post-reload verification (`shc-frr-check`,
  realization `nodes.frrLoadCheck`) computes it, read without writing its note; the lines that note keeps (FRR did
  not take them after two loads) are reported as `ConfigurationNotTaken` and never wake a run;
- the IPv6 egress as the root's own file names it (the hand-off address, the filter table, the upstream sessions,
  the default route in the egress VRF and in each network VRF with IPv6 egress), and a gateway's site sessions.

Every run writes `status.health.lastRunAt` on the HybridFabric, also a run that left the nodes alone;
`status.health.checkedAt` is when the nodes were last read. It writes `status.health` on the HybridFabric (the root's
findings and the node-wide units; every run) and on each
CloudGateway (its instance; only when something changed), the conditions `NodesHealthy` (the worst finding) and
`Degraded` (True while a finding reduces service: an unreachable node, a failed start or service, a routing daemon
down, a start without IPv6, no site session), and refreshes the IPv6 egress state
(`status.realization.egress.ipv6`, condition `IPv6EgressUpstream`) and the pool rows
(`status.realization.ipam.pools`). A finding counts in the conditions, events and wakes
(`confirmed`) at once when it is a lasting state (a start that failed, a unit failed or not started at boot), and only
once a second consecutive check found it when it may be a moment (a node not answering, a unit restarting by itself,
a daemon in watchfrr's restart); `status.health` lists it from the first check, with `checks`, and `healthy` is
false as soon as anything is listed. An instance whose configuration did not start reports that, not the drift it
causes, and the gateways on a node whose own configuration did not start report it too. Each node row carries its
worst counted finding (`reason`, `message`), which the alerts name together with the node. A
read that did not complete on a node that answered is `ReadFailed`, not `NodeUnreachable`. A gateway whose site is
paused reads `NodesHealthy` Unknown/`SitePaused`; one not realized on the nodes yet, or with a change of its own in
progress (its phase not Ready or Degraded, an event of its own in flight), Unknown/`NotChecked`. For a fabric whose
nodes are off on purpose, turn the check off for it (realization `fabrics.<name>.health.enabled: false`): its
objects read Unknown/`NotChecked` and the stale alerts stay quiet. Two alerts watch the check itself:
`ShcFabricHealthStale` when no run reported (`lastRunAt`) for `ops.fabricHealth.alerts.staleIntervals` intervals (the
schedule, AAP or the job is broken), and `ShcFabricNodesNotRead`, lower, when runs report but have not read the nodes
(`checkedAt`) for `ops.fabricHealth.alerts.notReadIntervals` intervals because changes to the nodes held them off. Events
come on transitions only (`NodeStartFailed`, `NodeServiceFailed`, `RoutingDaemonDown`, `RoutingDaemonRestarted`,
`ConfigurationDrift`, `SiteSessionsDown`, `ServiceSessionsDown`, `ServiceLinkDown`, `NodeUnreachable`, `NodeRebooted`,
`NodeHealthRecovered`).

What a full run repairs wakes the job of the object it belongs to (the fabric for the root, the gateway for its
instance) when that object is ready: a failed or inactive start, a unit not running or not started at boot at once,
drift once `health.wakeAfterChecks` consecutive checks found it; not again within `health.wakeBackoffMinutes`. An
unreachable node, a daemon crash or site sessions down wake nothing: a full run would not repair them. A change of
the nodes without IPv6 upstream wakes the networks with IPv6 egress; more free blocks in a pool (blocks leaving
hold-down) wake the networks waiting for platform address space.

## The service VRF

With `service.enabled` and at least one edge connection of the platform kind planned, the root holds one more VRF,
the service VRF (`service.vrf.name`, table `service.vrf.table`), through which the platform's management reaches the
OpenShift clusters it builds on tenant networks ([platform access](platform-access.md)). It has no VPN, route
target, SID or label: it never reaches the core and no tenant network imports it.

| Object | Name | When |
|---|---|---|
| Service VRF | `service.vrf.name` | the service is on and a platform edge connection is planned |
| Platform edge connection device | `<bearer interface>.<vlan>`, in the service VRF | per platform edge connection |
| Network root VRF | `e<vpn target>` | as for egress and circuits, and while the network has an accepted cluster address |
| Service veth pair | `se<vpn target>` (in `e<t>`) and `so<vpn target>` (in the service VRF) | while the network has an accepted cluster address |

A network's objects come with its first cluster and go with its last; a network without clusters has none of them,
whatever its platform access says. Each pair takes one /31 from the upper half of `egress.linkPool` by the network's
root index (egress links keep the lower half); the addresses are next hops only, forwarded packets keep their
sources. So with the service on there are at most as many root indexes as one half of `egress.linkPool` has /31s (and
no more than the smallest NAT pool, as before). Where that half is the limit and a network already holds a higher
index, turning the service on is refused by the fabric's ledger ("pool change refused: egress 0-<end> would drop
allocated [...]"); a larger `egress.linkPool` resolves it, nothing is renumbered.

Routes: in the service VRF, each accepted cluster address of a network via that network's `se` end (FRR static) and
the service range's routes from the platform edge connections (eBGP, filtered to `service.range`: the hub's
addresses the service VRF carries, inside `infrastructure.hub`). The network's VRF `e<t>` learns them from the service
VRF over its service link: an iBGP session between the two VRF
instances on the node (the network side connects, the service VRF has a router ID of its own, the node's address on
its first platform edge connection; TTL 255 only, `ttl-security hops 1`, and the root's input filter admits it from the
link's other end only). The service VRF sends the service range's routes and nothing else (next hop its `so` end),
the network VRF takes the service range and nothing else (weight 100, so its own beats the other node's in the
network's VPN) and sends nothing. So the service range enters the network's VPN from a node only while that node holds it from a
platform edge connection: a node whose connections are all down withdraws them, and the sites return the clusters'
traffic to the node that can deliver it. The failover time is the time the node needs to see its sessions go down
(at once when the connection's link goes down, at most the hold time of 30 seconds otherwise, a second with BFD on
the connection) plus about a second for the withdrawal through the network's VPN. With every connection of every
node down, no node announces the service range: the clusters lose the hub and nothing else changes (a reject route at
distance 254 for each `service.range` entry in `e<t>` keeps the clusters' traffic to it off the network's other
routes, its internet egress default among them). The service VRF's sessions announce the accepted addresses only,
never inside a published reserved range and, when `service.attachableRanges` is set, only inside it; they accept
routes inside the service range only, and never a default route, the link pools, the core pools or an accepted
address.

What the networks and their sites see of the hub's routes: the hub's routes enter the service VRF without any
community of the hub's router (standard, extended and large communities are removed on the platform edge connection:
a route target among them would import the hub's ranges into another network's VRF, `no-export` or `no-advertise`
would keep them from the networks) and without the hub router's own AS number (the edge connection's peer AS is
removed from the AS path). The AS numbers the hub's router learned them through, if any, stay in the AS path: a
tenant's site sees them behind the fabric's AS, and a site whose eBGP peer uses one of those numbers drops the hub's
routes as an AS path loop. FRR 8.5 removes AS numbers only by name (`set as-path exclude <asn>`; there is no "all"),
and replacing every AS number would put the fabric's own AS in the path, which the other nodes then drop as a loop.
So a deployment whose hub routes carry further AS numbers keeps those numbers off its tenants' sites' peers, or has
the hub's router originate (or aggregate) the hub's ranges itself.

`service.range` is required with `service.enabled` (the fabric fails with `InvalidSpec` naming it).
`service.attachableRanges` is optional. Each platform edge connection's `status.ceConfig` gives the hub's router
its filters in order: `acceptedRanges` (what it announces: the service range), `refusedRanges` (every published
reserved range, every site's own included, as `<range> le 32`: refused from the platform at any length), `advertisedRanges` (then accepted:
`0.0.0.0/0 le 32` without `attachableRanges`, else each of them as `<range> le 32`) and `maxPrefixes`
(`service.maxPrefixes`, which the plan never exceeds: later addresses wait). The nodes guard their own announcement
the same way (prefix list `SVC-ATTACHABLE` on the redistribution). Without `attachableRanges`, a cluster address is
any address the reserved ranges leave, so those ranges must cover everything the hub's network routes itself: a
more-specific route from the platform inside one of the hub network's own ranges would draw that traffic into a
tenant network (see [Platform access](platform-access.md#the-refused-ranges-or-an-allow-list)). What is announced
is bounded further per cluster: an OpenStack cluster's addresses are /32s (its floating IPs and its router's
gateway), an AWS or Google Cloud cluster's the subnet of its placement on the network, each inside the network's
accepted address space; anything else is refused on the network's prefix row.

The table `ip shc_service`, loaded before FRR takes the service VRF and replaced whole on every run, filters what
crosses it, statelessly (the two directions may cross different nodes): from a platform edge connection, sources
inside `service.range` only (set `service_range`), TCP on `service.ports` and ICMP echo to a network's accepted
addresses only; from a network's link, by destination, packets to `service.range` only (replies from the service
ports, TCP to `service.hubEndpoints`, ICMP errors and echo replies), no rule naming a client range, never from a
service link's own addresses (the upper half of `egress.linkPool`), and only from a source the service VRF routes
back over that same link (a network's own accepted addresses). How that last check is made is one switch at the top
of `shc-fabric-apply`, `SERVICE_SOURCE_CHECK`: `fib` (default) puts a reverse path rule in the table (`fib saddr .
iif oif missing`; it needs a kernel whose nftables `fib` looks a VRF slave's source up in the VRF's table, upstream
6.2, else every cluster packet is dropped), `rp_filter` sets the kernel's strict reverse path filter on each `so*`
link instead (set and checked on every run; sound there because those links carry nothing decapsulated, unlike the
network VRFs, which run without it on SRv6); nothing from
one `so*` device to another (the drops match the `so*` and `se*` devices by name, never by a list, so a pair a run
has not yet removed is dropped too), so no network reaches
another through the service VRF. The service VRF joins the root's input filter (`shc_root_in`): its connection
devices are legs whose input `shc_service` filters itself (BGP, BFD and ICMP from the connection's link only,
everything else dropped), and the `se*`/`so*` pairs get the filter's common answers. Each dropping rule counts its
packets (`nft list table ip shc_service`).

What the stateless filter cannot tell apart, and what remains of it: a reply is recognised by its shape (TCP from a
service port with ACK set), not by a connection the hub opened, because the request and its reply may cross
different nodes. So a cluster can send ACK-shaped segments (also with RST, FIN or data) from its own accepted
addresses and a service port to any address and port in the service range. Nothing new is opened by them: a hub host
with a stateful stack or firewall drops them or answers with a reset. Those resets come back to the cluster (they are
addressed to a service port), so a cluster could map which hub addresses answer; the filter passes the hub's resets
toward a network at no more than 10 per second (burst 20) per network and node, which slows such a scan down to a
trickle without touching the hub's own connections. The cluster can never use another network's addresses (the
reverse path check), never reach a hub port with a SYN other than a hub endpoint's, never reach a hub address outside
`service.range`, and never reach another network. Hub hosts that must not be seen at all from the clusters belong
outside `service.range` or behind a firewall that drops unsolicited ACKs silently.

The health check reads `show bgp vrf <service> summary json` and reports `ServiceSessionsDown` on the fabric when a
node has no established session on a platform edge connection (confirmed on a second check; it wakes nothing: the
node has withdrawn the service range from the networks already, the finding is for the platform team). The sessions
over the service links do not count for it: the same read gives the session of each network's service link (the
service VRF to the network's VRF), and one that is not established while the node's platform edge session is up is
`ServiceLinkDown` on the fabric, naming the network (confirmed on a second check, degrading, alert
`ShcFabricServiceLinkDown` after `ops.fabricHealth.alerts.serviceLinksFor`). With every platform edge session of the
node down, only `ServiceSessionsDown` is reported. It wakes nothing either: a full run repairs only a missing link or
neighbour, which the drift check reports and wakes for already. The
apply's snapshot covers the VRF, the pairs and the table, so drift is reported as for the rest of the root.

## The orphan check on a node

The fabric orphan check (report only) reads every border gateway node of a fabric. Besides the namespaces, VRFs,
circuit devices and BFD sessions that no object accounts for, it reports what the apply creates that belongs to no
VRF or instance any more, judged by the fabric's core transport:
- on both transports: an IPv4 or IPv6 unreachable route at the apply's metric in a table no VRF uses (a VRF removed
  behind the jobs), an ICMPv6 source address on a device that is no VRF, and an IPv4 `lookup local` rule anywhere but
  at 32765 (a namespace where a VRF reaches the default VRF's addresses), and the root VRFs' input filters judged
  against the root's VRFs and the dual-stack core: `ip shc_root_in` or `ip6 shc_root_in6` loaded with no root VRF
  (or the IPv6 one without the dual-stack core) is left over, root VRFs without their table are reported as
  unprotected;
- on `srv6`: the SID block's unreachable route and rule for another block than the fabric's, a guard unit or guard
  file of an instance that is not on the node, and MPLS routes or the MPLS module file (left from `mpls`);
- on `mpls`: every SRv6 piece (the block's route and rule, the guard table, an encapsulation source, the guard units
  and files), left from `srv6`.

The state the apply creates for the fabric's own transport and instances is not reported.

## Teardown

A gateway's teardown stops and disables its unit, removes its namespace and core leg, its FRR instance and its files.
The fabric's teardown, once its gateways are gone, stops and disables FRR, turns off the daemons the fabric turned on
(`bgpd`, `isisd`, `bfdd`), leaves FRR a configuration without the fabric, removes the IPv6 egress guard (its timer
and files) and the SRv6 core guard units, and removes the root instance and its files. A run of the fabric
removes the `se*`/`so*` pairs of networks that no longer have cluster addresses, and the service VRF, its devices
and `ip shc_service` once no platform edge connection is listed; the root's teardown removes whatever of them is
left with the root instance.

The last step of either teardown checks whether any instance of the fabric is left on the node: a root or gateway
configuration, or a gateway unit that runs, has failed or is enabled. When none is, it stops and disables the root
unit and removes the apply script, the unit templates and the empty configuration directory, so nothing of the
fabric stays installed, enabled or running. While an instance is left, all of that stays: that gateway's own
teardown needs the script, and its unit requires the root unit. The next job that sets the node up installs them
again. Running a teardown a second time changes nothing.

What stays on purpose: FRR itself, disabled, with its own configuration files; on `srv6` the sysctls
`net.vrf.strict_mode` and `net.ipv6.seg6_flowlabel` (inert without SIDs); on `mpls` the MPLS kernel modules until the
next boot (they are no longer loaded at boot).

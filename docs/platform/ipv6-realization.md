# IPv6 realization

For the platform team. How tenants use IPv6 is in
[IPv6 for a hybrid network](../../architecture/docs/tutorial/09-hybrid-networks.md#ipv6-for-a-hybrid-network); this
page describes the realization parameters behind it. The objects (`HybridNetwork`, `NetworkPlacement`) state the
tenant's intent only. Which address space the platform hands out, which sites offer IPv6 and how is realization: it
lives in the EDA vars `common/vars/fabric_realization.yml` (the generic defaults, shipped off and empty) and in the
installation's `FABRIC_REALIZATION` (the aap-config environment, JSON; per fabric under
`fabrics.<fabric name>`). Installation values never go into Git, the CRDs or the forms
([Lab configuration policy](../lab-config.md)).

With the defaults nothing changes for IPv4, every IPv6 placement fails with `FamilyUnavailable`, a network asking
for platform IPv6 fails with `FamilyUnavailable` (no pool), and IPv6 internet access reads `NotOffered` (platform
IPv6) or `AddressChoice` (tenant prefixes). The border gateways' core stays IPv4-only, and the gateways refuse every
IPv6 route a site announces (no placement holds IPv6 yet; see [Transport](#transport-dual-stack-core)).

## The three network modes and what the platform does for each

| Mode (`status.ipv6.mode`) | Tenant writes | The platform |
|---|---|---|
| `Tenant` | IPv6 entries in `prefixes` | checks them against the exclusive ranges and the tenant's other networks, then allocates placement subnets from them like IPv4; no internet path in either direction |
| `Platform` | `platformPrefixes` with `family: 6` | allocates one block from a public pool of the fabric (level 1), then placement subnets from the block (level 2) |
| `None` | neither | refuses `ipv6` placements (`FamilyUnavailable`: the network has no IPv6 address space) |

The mode is derived from the fields; the network job keeps the mode it last realized and refuses a change while the
network has placements (`IPv6ModeInUse`), without touching what is realized.

## Address pools (`ipam`)

```yaml
ipam:
  provider: ledger          # where address spaces and subnets are kept (the fabric's ledger)
  holdDownDays: 7           # default hold-down for pools that set none
  publicPools: {}           # none by default: no platform IPv6
  # publicPools:
  #   <pool name>:
  #     family: 6
  #     prefixes: [<the pool's prefixes>]
  #     reserved: []              # ranges inside the pool that are never handed out
  #     blockLengths: [56, 60]    # block sizes a network may ask for (product range /48 to /64, steps of 4)
  #     defaultBlock: 56          # the size when the network names none; one of blockLengths
  #     holdDownDays: 7           # days a released block stays out of use
  #     perTenantBlocks: 4        # blocks one tenant may hold at once; absent = no limit
  #     perTenantOverrides: {}    # <tenant name>: <blocks>
```
- **Level 1, pool to network block:** `ipam-pool-ensure` writes every pool of the realization into the fabric's
  ledger. It is idempotent and runs on every HybridFabric run, before every pool allocation of a network, and before
  every release of a block (network teardown, a network leaving mode `Platform`). A realization change (prefixes,
  sizes, hold-down, allowance) therefore reaches the ledger at the fabric's next run (Retry now on the HybridFabric
  starts one) or at the next network that allocates or releases a block, whichever comes first. A
  malformed or refused pool realization fails the network job that asks for a block; the fabric job and the
  releases go on with the pools as the ledger holds them, and the fabric reports why under
  `status.realization.ipam.refused`. Then the network job runs `ipam-pool-allocate` for its `platformPrefixes` entry:
  first fit at the lowest aligned address, or the pinned block. The block is recorded in the ledger section
  `ipamPools` under the owner `HybridNetwork/<namespace>/<name>`.
- **Level 2, block to placement subnet:** unchanged from IPv4: the network's address space is its `prefixes` plus its
  block, tagged by source (`status.addressSpace[].source`), and placements take the lowest free subnet of their size,
  stepping around the site's reserved ranges.
- **Refusals and waits** (status reasons in [Status reasons](../tenant/status-reasons.md#reasons)):
  - no pool of the family: `FamilyUnavailable`;
  - a size not in `blockLengths`, a pinned block outside the pool or on a reserved range: `PrefixInvalid`;
  - the tenant's allowance used, no free block, only held-back blocks would fit, or the pinned block held by another
    network: `WaitingForAddressSpace`, with a message that says which (another tenant's network is never named) and
    `status.ipv6.holdDownUntil` while a held-back block is the one wanted. A release wakes the waiting networks.
- **Hold-down:** `ipam-pool-release` (network teardown, or a network leaving mode `Platform` without placements)
  stamps the block with the release time and the former owner; the block is free again after the pool's
  `holdDownDays`. A hold-down of 0 frees it at once. The point is that a new owner never inherits the old owner's
  reverse DNS records, remote allow-lists or address reputation.
- **Teardown order:** placement releases, then the network's address space (`ipam-drop`), then the block
  (`ipam-pool-release`).
- **Pool use** is reported in `HybridFabric.status.realization.ipam.pools` (per pool: family, offered sizes, default
  size, blocks used, free and held back, largest free block), written by every fabric run and after every pool
  change. A fabric whose realization has no pool reports an empty list. The console's network form reads the
  offered sizes and the default from there and, for users who can read the HybridFabric, preselects **From the
  platform** when a family-6 row is present.
  The rows are those of the fabric's last run or last pool change, so after adding a pool to the realization run
  Retry now on the HybridFabric: until its next run the form keeps preselecting **None** and says the fabric reports
  no platform IPv6 space (a network that asks for a block still gets one: its own job takes the new pool). A fabric
  status without rows (a fabric that has not run yet) makes the form preselect **None** without that line. A pool
  change on a fabric run (more blocks, a larger allowance, a shorter hold-down) wakes the networks waiting for
  platform address space.
  Tenants cannot read the HybridFabric: the Entity job copies the offered sizes and the default of the family-6 row
  into the tenant's `TenantProfile` `status.ipv6Platform`, and the form preselects **From the platform** from
  there. It does so when exactly one fabric is open to the tenant (its `entityRefs` empty or naming the tenant);
  with none or several, or a fabric without a family-6 row, the field is absent and the form preselects **None**.
  The fabric keeps the offer it last announced to the tenants in `status.realization.ipam.tenantOffer` (in words,
  for example `/48 /56 default /56`, or `none`); a fabric run whose offer differs from it (also the first run that
  writes it), a change to the fabric's spec, and a fabric teardown wake the Entities, so the profiles follow. The
  fabric stores the offer as told only when it could list the Entities for the wake; otherwise its next run wakes
  them again (a tenant's profile is rewritten only by a woken or changed Entity).
- **Several fabrics:** give each fabric's ledger a disjoint slice of the address space.
- **Ownership records:** the ledger keeps each block's owner (and, for a released block, its former owner and the
  release time) and each provider-assigned placement subnet under `external` in the network's IPAM state, so a block
  or subnet without a live owner can be traced to it.
- **`fabric-orphan-check`** reads the pool section too: it reports pool blocks whose network is gone, a block
  missing from its network's address space, and provider-assigned subnets whose placement is gone.

## Exclusive ranges (`network.exclusiveRanges`)

```yaml
network:
  exclusiveRanges: []   # prefixes no tenant prefix may overlap, in either direction
```
A tenant prefix that overlaps an entry (either way round) is refused with `PrefixReserved`. Every pool's prefixes are
added automatically. List here the rest of the platform's own space: the installation's whole public IPv6
allocation, infrastructure ranges, and the translation prefixes the sites use. A tenant GUA outside these ranges is
accepted as the tenant's own; the platform can't check ownership, and mode-1 prefixes never reach a shared path.

Overlap between networks: inside one network prefixes may not overlap (checked at admission); a tenant's own IPv6
prefixes may not overlap another network of the same tenant (`PrefixInUse`); networks of different tenants may
overlap or repeat, since each network is its own routing domain.

## Placements (`placement`)

```yaml
placement:
  ipFamilies: {CloudVirt: [4], CloudOSO: [4], CloudAWS: [4], CloudGCP: [4]}   # families per environment type
  ipFamiliesBySite: {}      # <site>: [4, 6]; overrides ipFamilies for that site
  ipv6OnlyBySite: {}        # <site>: true when IPv6-only placements are offered
  ipv6SourcesBySite: {}     # <site>: [Network, Provider]; overrides ipv6.sources for that site
  ipv6:
    sources: {CloudVirt: [Network], CloudOSO: [Network], CloudAWS: [Network, Provider], CloudGCP: [Provider]}
    defaultSource: {CloudAWS: Network, CloudGCP: Provider}   # applied when a placement names no source
    defaultPrefixLength: {CloudAWS: 60}                      # applied when a cloud placement names no size
    tenantPrefixes: {CloudGCP: false}                        # environment types that can't carry tenant IPv6
    addressMode: {CloudOSO: dhcpv6-stateful, CloudVirt: dhcpv6-stateful}   # how placement subnets hand out addresses
  resolversBySite: {}       # <site>: [addresses]; resolvers handed to the site's placement subnets, per family
  siteReserved:             # per environment type; the defaults hold the product's own ranges
    CloudVirt: [100.64.0.0/16, 100.65.0.0/16, 100.88.0.0/16, 169.254.0.0/17,   # OVN-Kubernetes internal subnets
                fd69::/112, fd97::/64, fd98::/64, fd99::/64]                   # and their IPv6 counterparts
    CloudOSO: []
    CloudAWS: [fd00::/16, 2001:db8::/32]   # ranges Amazon keeps for itself in a VPC's IPv6 space
    CloudGCP: []
```
- **Families offered at a site** = `ipFamiliesBySite[site]`, else `ipFamilies[environment type]`, else `[4]`, and
  always ANDed with the site's probe (OpenShift: both families in the cluster network status). IPv6-only and the
  sources follow the same pattern. A request the site doesn't offer fails with `FamilyUnavailable`, with a message
  that names the site and the missing piece, before anything is allocated.
- **Switches stay off until the site's IPv6 data path is in place** (the fabric transport, the site gateway and the
  site's own IPv6 configuration). A switch turned on for a site whose data path isn't ready makes placements that
  can't carry traffic.
- **Sizes and sources are product facts** checked at admission: /64 on VM and OpenStack environments; /56 or /60
  with source `Network` on AWS (a VPC takes IPv6 blocks of /44 to /60), no size with source `Provider` (the provider
  assigns the range); `Provider` only on AWS and Google Cloud. The job checks the network's mode first and applies
  the default source after, so a placement of a tenant-prefix network on Google Cloud is refused for the mode, never
  for a defaulted source.
- **`siteReserved`:** the allocator steps around these ranges (besides the site underlay and, on OpenShift sites,
  the cluster and service networks) and refuses a pinned prefix on them (`PrefixInvalid`). A tenant network whose
  IPv6 space lies wholly inside one of them can't be placed on that environment type (`PrefixInvalid`). Override
  the list only when a site's product version uses other internal ranges.
- **`addressMode`:** per environment type, how the workloads of a placement get their IPv6 addresses. On OpenStack
  sites it is applied: every IPv6 placement subnet is created with it as RA mode and address mode (empty = the
  site's own default), and it can't change on an existing subnet (Neutron fixes both at creation; a changed value
  reaches new subnets only). On OpenShift sites it is informational: the cluster network hands out addresses by
  stateful DHCPv6. `dhcpv6-stateful` is the only OpenStack mode in which resolvers reach a guest by protocol; the
  config drive carries them in every mode. The placement's status reports the resulting addressing
  (`status.ipv6.addressing`: `DHCPv6` or `SLAAC`).
- **`resolversBySite`:** per site, the resolver addresses its placement subnets hand to the workloads. On OpenStack
  sites they become each subnet's `dns_nameservers`, by family (IPv4 addresses on the IPv4 subnet, IPv6 on the
  IPv6 subnet); a subnet whose list differs is updated on the next run, so a change reaches existing placements. A
  site not listed gets an empty list (enforced on existing subnets too), so its workloads fall back to the site's
  default, usually the site's own resolver, which a placement's workloads may not reach through the fabric; list
  resolvers the network's egress can reach. OpenShift sites take
  no resolvers from here (the cluster's DNS serves the VMs).
- **OpenStack placements** (CloudOSO): the placement's network holds one subnet per allocated prefix, IPv4 first,
  on one EVPN router whose VNI is the placement's. The first subnet attached to the router carries the site's host
  advertisement flag and the others attach plainly: the site's EVPN plugin accepts one advertised interface per
  network and advertises the host routes of every subnet regardless. The site therefore sends /128 host routes for
  the IPv6 subnet (never the /64), which the gateway's site-import filter accepts inside the placement prefix. A
  re-run creates nothing; deletion removes every router interface, then the router, the subnets and the network,
  and waits while tenant ports remain on the network (`deletionBlockedBy`).

## Site capabilities

A site offers IPv6 placements when two things agree: what the site's probe found it can carry, and the switch the
realization sets for that site. The site job (`cloudinfrastructure-provision`) writes the result into
`CloudInfrastructure.status.capabilities`, each key the probe ANDed with its switch; all are `false` while the
switches are off:

| Key | Meaning | Probe | Switch |
|---|---|---|---|
| `ipv6` | IPv6 placement subnets can be carried | see below | `placement.ipFamiliesBySite.<site>` holds 6 (else `placement.ipFamilies.<environment type>`) |
| `ipv6Only` | IPv6-only placements are offered | as `ipv6` | `placement.ipv6OnlyBySite.<site>: true` |
| `ipv6NetworkSource` | subnets can come from the network's IPv6 space | as `ipv6`; never on Google Cloud | `Network` in `placement.ipv6SourcesBySite.<site>` (else `placement.ipv6.sources.<environment type>`) |
| `ipv6ProviderSource` | the cloud provider can assign subnets | a cloud site | `Provider` in the same list |
| `ipv6Translation` | IPv6-only placements reach IPv4 by translation | as `ipv6` | a `translation.<site>` entry |

The probe, per site type: an OpenShift site is dual-stack (its `network.config.openshift.io/cluster`
`spec.clusterNetwork` has an IPv4 and an IPv6 entry); an OpenStack site has EVPN (its IPv6 subnets ride the same
path as IPv4); AWS and Google Cloud offer IPv6 (Google Cloud from the provider only); an external site offers
nothing. The probe is read on every run of the site job; a cluster converted to dual-stack shows it at the next run.

**Where the switches live.** The site job reads the `placement` and `translation` switches from the **top level** of
`FABRIC_REALIZATION` only: a site belongs to no single fabric, so the job never merges a `fabrics.<fabric name>`
section (the fabric, gateway, network and placement jobs do merge it on top of the top level). Keep these switches
at the top level. A switch written only under `fabrics.<name>` would let the placement job's own check pass while
the site still reports `ipv6: false`, so the placement is refused all the same (`FamilyUnavailable`) and the site
status does not say why.

**Switching a site on.** The site's IPv6 data path comes first: the dual-stack core and its gateways
([Transport](#transport-dual-stack-core)), and the site's own IPv6 (a dual-stack cluster, or an OpenStack site with
EVPN). A switch turned on before that makes placements that cannot carry traffic. Then:

1. Add the site to `placement.ipFamiliesBySite` at the top level of `FABRIC_REALIZATION`, for example
   `{"placement": {"ipFamiliesBySite": {"<site>": [4, 6]}}}` merged into the installation's value (the other
   switches the same way, only when the site is meant to offer them).
2. Let the value reach the automation: `FABRIC_REALIZATION` is an environment value of the automation's inventory
   (`fabric_realization`), so the aap-config and the activations that carry it have to pick the new value up.
3. Run the site job: Retry now on the `CloudInfrastructure` (the `reconcileNow` annotation,
   [Status reasons](../tenant/status-reasons.md#retry-now)), or wait for its next periodic run. Check
   `status.capabilities.ipv6: true`. The job also wakes the tenants whose site view changed, so their profiles
   follow.

**What tenants see.** The capabilities reach the tenant profile's site list (`IPv6`, `IPv6Only`, `IPv6NetworkSource`,
`IPv6ProviderSource`, `IPv6Translation`), so the forms show the IPv6 inputs only where a site offers them; the
command line is not gated, the job's `FamilyUnavailable` stays the authority. On a switched site a placement with
`ipv6` is realized with a family-6 row in `status.subnets` and a `status.ipv6` block: `addressing` (how workloads get
their addresses, `DHCPv6` or `SLAAC`, from `placement.ipv6.addressMode` of the environment type) and `internet`
(`{state: unavailable, reason: NotOffered}` while `egress.ipv6.enabled` is off; the egress realization fills the
ready case). On a site not switched, or for a request the site does not offer (IPv6-only, a source), the placement
fails with `FamilyUnavailable` and a message naming the site and the missing piece, before anything is allocated.

**Switching a site off.** Take the site out of `placement.ipFamiliesBySite` (or set it to `[4]`) and run the site
job again: new IPv6 placements are refused. Existing placements keep their IPv6 subnet (a placement's families are
fixed at creation) until they are deleted; a converted cluster stays dual-stack.

## Translation (`translation`)

```yaml
translation: {}   # <site>: {pref64: <the site's translation /96>, dns64Resolvers: [<IPv6 resolver>, ...]}
```
Translation follows from the network: IPv6 from the platform, no IPv4 and `egress.internet` on. There is no tenant
switch. Dual-stack networks never get DNS64 resolvers. With no entry for a site, `status.egress.ipv4Translation` reads
`unavailable` (`NotOffered`).

## IPv6 internet egress (`egress.ipv6`)

```yaml
egress:
  ipv6:
    enabled: false         # routed IPv6 egress for platform-assigned space; off: IPv6 internet reads NotOffered
    standbyCommunity: 998  # the standby node's default route carries <domainAsn>:998 ...
    standbyLocalPref: 50   # ... and reaches the sites with this local preference (they prefer the primary node)
```
IPv6 egress is routed, not translated: a network's platform block leaves unchanged through a stateful filter on the
border gateway nodes that admits only replies to connections opened from inside. Tenant-provided prefixes never get
an internet path. While `enabled` is false every network in mode `Platform` reports
`{family: 6, state: unavailable, reason: NotOffered}` in `status.egress.families`, and `status.egress.internet`
keeps reporting the IPv4 state of networks with IPv4.

## Transport (dual-stack core)

IPv6 between the sites rides the border gateway core as IPv6 VPN routes next to the IPv4 ones. The core is
dual-stack: every root and gateway namespace on a border gateway node gets an IPv6 loopback in the same IS-IS
(single topology, one prefix SID per loopback), the core legs carry IPv6 link-local addresses only, and a VRF's IPv6
VPN routes use the namespace's IPv6 loopback as next hop. FRR installs no labelled IPv6 route over an IPv4 next hop,
so an IPv4-only core can't carry IPv6 between sites. The BGP sessions stay on the IPv4 loopbacks.

```yaml
core:
  loopbackPool6: ""   # the namespaces' IPv6 loopbacks; empty = IPv4-only core (nothing IPv6 is rendered or applied)
  sidOffset6: 1000    # SR index of an IPv6 loopback = the namespace's IPv4 index + this offset
  icmpSource6: ""     # source of the ICMPv6 errors the VRFs send; one /128 per namespace per node
gateway:
  siteImportFilter:   # per address family: only the placements' prefixes enter a VRF from the site
    ipv4: false
    ipv6: true
```
- **`core.loopbackPool6`:** an IPv6 network of at least a /120, never advertised outside the core (the egress
  filter and the upstream routers should drop it). Provider edge `i` (the root is 1, the gateways 2 and up) gets
  address `i` of it, the same on every node: each node is its own core. Example with documentation prefixes:
  `2001:db8:4005:ffff::/64` gives the root `2001:db8:4005:ffff::1` and the gateway with index 2
  `2001:db8:4005:ffff::2`.
- **`core.sidOffset6`:** an IPv6 loopback's SR index is its IPv4 index plus this offset, so it must be larger than
  the highest provider-edge index, and offset plus that index must stay inside `core.srgb`. The HybridFabric job
  refuses the fabric (`InvalidSpec`) otherwise.
- **`core.icmpSource6`:** an IPv6 network of at least a /96, apart from `loopbackPool6`. Every VRF device of a
  namespace gets `network + (node position << 16) + provider-edge index` as a /128 (the node position is 1-based in
  `borderGateway.nodes`), so the kernel can send Packet Too Big, Time Exceeded and Unreachable from inside a VRF.
  Example: `2001:db8:4005:1::/64` gives the root on the first node `2001:db8:4005:1::1:1` and the gateway with index 2
  on the second node `2001:db8:4005:1::2:2`. These addresses are never announced. Take them from a range the
  platform routes (an ICMP error with an unroutable source never reaches the sender).
- **`gateway.siteImportFilter`:** the gateways' import filter on the routes a site announces (`FROM-SITE`). For a
  family that is on, a route of that family enters only when its prefix lies inside one of the prefixes of the
  site's placements on that gateway (host routes inside them included); every other prefix of that family is
  refused, so a site router can't put another prefix into a tenant's network. Routes without an IP prefix
  (inclusive multicast, MAC-only routes) and the other family pass. The gateway is also the route reflector of the
  site's own nodes, so a refused route doesn't reach the site's other nodes through the gateway either. IPv4 is off
  by default: the gateways then refuse only their own routes coming back, as before.

**What a run applies.** With `loopbackPool6` empty the jobs render the core as before. Two things change for every
core: each network's VPN export map is its own (`TO-VPN-<route target>`, it sets the network's route target again,
which FRR 9 and later need), and `FROM-SITE` carries the IPv6 entries of the site-import filter. With
`loopbackPool6` set:
- the HybridFabric job, per node: the root's IPv6 loopback, IPv6 on the core bridge (link-local only, router
  advertisements ignored), IS-IS for IPv6 with the loopback's prefix SID, and the IPv6 VPN family on the route
  reflector. `status.realization.routeReflector6` names the root's IPv6 loopback, `status.realization.ipv6Core` is
  true.
- the CloudGateway job, per node: IPv6 forwarding in the gateway namespace with router advertisements ignored, IPv6
  on the core leg (link-local only), none on the site leg, the IPv6 loopback in IS-IS, and the IPv6 VPN family on
  the session to the route reflector, also while no placement holds IPv6. A VRF whose placement holds IPv6
  (`status.realization.vrfs[].families` shows `6`) gets the IPv6 unicast family with the IPv6 loopback as VPN next
  hop and announces its IPv6 routes to the site. `status.realization.loopback6` names the gateway's IPv6 loopback.
- both: an `unreachable` IPv6 default route in every VRF table (a miss fails inside the VRF instead of falling
  through to the node's main table), and the `icmpSource6` address on every VRF device when that is set.

**Rollout.** A new BGP address family resets the sessions it rides on. Set the `core` keys in `FABRIC_REALIZATION`,
then run the HybridFabric (Retry now) first, so the route reflector offers the IPv6 VPN family, then each
CloudGateway. A gateway takes the IPv6 core only once the HybridFabric reports `status.realization.ipv6Core`
(written after the root passed on every node): a CloudGateway run before that, or after a HybridFabric run that
failed between the nodes, keeps the gateway as on an IPv4-only core. Every job configures one node at a time and
waits for IS-IS, the VPN sessions and the VPN labels on it before the next, so the other node carries the sites
meanwhile. Per node, the root job resets its VPN sessions once (every gateway's session to the root on that node),
and each gateway's session to the root resets once more when that gateway itself is re-rendered by its own job:
two resets per gateway session, one job at a time. Later runs change nothing. Do it while no placement uses IPv6.

**Rollback.** Empty `loopbackPool6` and `icmpSource6` and run the same jobs in the same order: the IPv6 families go
away, which resets the sessions as many times again. Emptying the pools removes the IPv6 loopback and ICMP source
addresses (each node remembers the pools of its last run until a run without them removed their addresses); the
IPv6 sysctls and `unreachable` defaults stay, inert: they don't affect IPv4.

- **`gateway.icmp6RateLimitMs`** (default 0): milliseconds between the ICMPv6 error messages a gateway namespace
  sends to one destination; 0 means no per-destination limit (the kernel's global limit still applies). While the fabric offers no IPv6 internet access, a site's off-network IPv6 traffic ends
  at the gateway VRF's unreachable default and is answered with "destination unreachable"; the kernel's default of
  one message per second per destination (even one every 20 ms) leaves a guest connecting to a name with many
  addresses waiting a full retransmit for the answers that were limited. Empty keeps the kernel default.

## Attachment circuits

```yaml
attachments:
  reservedAsns: []   # AS numbers a tenant's edge router may not use (the platform's own, e.g. its egress AS)
```
A circuit whose peer AS is listed fails with `PeerAsnReserved`. IPv6 entries in a circuit's accept list stay refused at
admission.

## Rollout and rollback

- Order: the primary chart with both CRD copies, then the EDA content (repository seed, then the AAP configuration,
  then restart the activations), then the UI images. The CRDs go first: an object that uses a new field can block
  its own CRD in Argo CD.
- Every CRD change is a relaxation or an optional field. To roll back, delete the objects that use the new fields,
  then pin the old chart. Old automation ignores the ledger's `ipamPools` section.

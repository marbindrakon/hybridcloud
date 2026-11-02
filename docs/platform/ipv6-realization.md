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

## Cloud sites: AWS placements

An AWS environment's placement is one VPC in the environment's account. With `spec.ipv6` it is dual-stack: the VPC
always keeps its IPv4 CIDR, and the IPv6 block comes from one of two sources (`spec.ipv6.source`; without one,
`placement.ipv6.defaultSource.CloudAWS`, shipped `Network`).

| | `Network` (the network's space) | `Provider` (Amazon's block) |
|---|---|---|
| Block | a /60 (`placement.ipv6.defaultPrefixLength.CloudAWS`) or a /56 from the network's IPv6 space, allocated like any placement subnet | the /56 Amazon assigns to the VPC |
| How it gets into the VPC | the account's IPAM in the region (Free Tier, one per account and region, private GUA enabled, tagged `shc-role: tenant-ipam`): a private-scope pool per placement that holds exactly the block, and the VPC's IPv6 block from that pool. AWS never advertises a private-scope block | an Amazon-provided block on the VPC |
| Subnets | per availability zone a /64 for the workload subnet (from the bottom of the block) and a /64 for the transit gateway attachment subnet (from its top); the attachment subnets keep their IPv4 /28s, an attachment needs IPv4 | the same, inside the /56 |
| Routes in the VPC | the network's IPv6 space (the network's `status.ipv6.prefixes`, less the VPC's own block) to the transit gateway; `::/0` to the transit gateway when the network has IPv6 from the platform and `spec.egress.internet` is on | the network's IPv6 space to the transit gateway; `::/0` to an egress-only internet gateway, created only while `spec.egress.internet` is on and removed when it goes off |
| Internet | through the fabric and the site gateway's IPv6 egress filter, with the workload's own address as source; tenant prefixes (mode `Tenant`) have none | AWS's own path, outbound only; never through the platform (the block is not in the fabric's egress filter) |
| `status.ipv6.internet` | the network's IPv6 egress row, with `path: Fabric` | `{state: ready, path: Provider}` while `spec.egress.internet` is on, otherwise none |

**What the fabric sees.** The edge routers learn the VPC's IPv6 block from the transit gateway (the placement's
attachment propagates into the network's route table there) and announce it to the site gateway as an EVPN Type-5
route in the placement's VRF, like the IPv4 prefix ([Transport](#transport-dual-stack-core); the routers' IPv6 is the
AWS realization's `edge.ipv6`). A provider-assigned block is also recorded in the network's address space as a
provider-assigned subnet (the IPAM's `external` entries, action `ipam-external`): the placement's `status.subnets`
then carries it with `source: Provider`, the gateway's route check waits for it like for an allocated prefix, and the
network's placements in VM and OpenStack environments reach it through the fabric. A cloud edge router never
announces another cloud site's provider-assigned block toward its own cloud, so two provider-source placements of one
network on different cloud sites reach each other over the internet, in both directions.

**What the tenant sees.** `status.subnets` has a family-6 row `{family: 6, prefix, source}` (no gateway: AWS keeps
the subnet's router addresses); `status.ipv6` has `addressing` and `internet` with its `path`;
`status.realization.site.ipv6` records `source`, `prefix`, the /64s per zone, the pool and IPAM (source `Network`) or
the egress-only internet gateway (source `Provider`), and the IPv6 routes of the VPC. A re-run creates nothing: the
VPC, its block and the routes are kept, and the VPC's IPv6 routes are set back to exactly the set above.

**Costs.** IPAM Free Tier is free, private GUA included. Source `Network` sends the placement's internet and
inter-site traffic through the fabric: the round trip to the site gateway adds latency, and AWS charges data
transfer out on the tunnel leg. Source `Provider` pays AWS's own data transfer rates through the egress-only internet
gateway (which has no hourly charge). The transit gateway attachment and the edge routers are shared with IPv4.

**Refused** (before anything is allocated or built):
- IPv6-only placements on a site without `placement.ipv6OnlyBySite` and the `ipv6Only` capability:
  `FamilyUnavailable` ("IPv6-only placements are not offered on this site"); on an installation without a placeholder
  pool, see [AWS: IPv6-only placements](#aws-ipv6-only-placements).
- A tenant prefix (mode `Tenant`) inside a range AWS keeps out of a VPC's IPv6 space
  (`placement.siteReserved.CloudAWS`: `fd00::/16`, `2001:db8::/32`): `PrefixInvalid`, the message ending "the cloud
  reserves this range". ULA outside `fd00::/16` and any GUA are accepted; AWS performs no ownership check on a
  private-scope block, so the network's own check of tenant prefixes
  ([Exclusive ranges](#exclusive-ranges-networkexclusiveranges)) is the only one.
- `source: Provider` on a network with tenant prefixes: `PrefixInvalid` ("provider IPv6 needs a network with IPv6 from
  the platform").
- Sizes other than /56 and /60 with source `Network`, and any size with source `Provider`: refused at admission.
- Bringing your own IPv6 block (BYOIPv6): not offered (no field).

**Teardown** goes in reverse: the VPC's IPv6 routes, the egress-only internet gateway, the attachment, the subnets,
the VPC's IPv6 block, the VPC, then the placement's pool (deleted with its contents: IPAM releases a VPC's
allocation several minutes after the VPC is gone, and a plain deprovision would wait for it) and the IPAM with its
last pool. The provider-assigned block leaves the network's address space with the placement's subnets.

**An IPAM the platform does not manage.** An account holds one IPAM per region by default. When the environment's
account already has one there that is not tagged `shc-role: tenant-ipam`, creating the platform's fails on the
account's quota and the placement fails with AWS's message; raise the quota or remove the other IPAM.

### AWS: IPv6-only placements

A placement without `spec.ipv4` on an AWS site that offers IPv6-only (`placement.ipv6OnlyBySite` and the site's
`ipv6Only` capability) is a VPC whose workloads have IPv6 only. Either source works; the block, its pool and the
network's IPv6 routes are as for a dual-stack placement above.

**The placeholder IPv4.** A VPC always has an IPv4 CIDR, and AWS refuses a transit gateway attachment on IPv6-only
subnets (the attachment's interfaces need an IPv4 address). The AWS realization therefore names a range for these
CIDRs, `tenant.placeholderV4Pool` (default empty):

```yaml
tenant:
  placeholderV4Pool: ""   # an IPv4 range never routed anywhere, /16 to /26; AWS must accept it as a VPC's primary
                          # CIDR (RFC 1918, 100.64.0.0/10 or publicly routable; not 172.17.0.0/16)
```
- Each IPv6-only VPC takes a /26 of it, chosen by the placement's layer 3 VNI (the same on every run), holding only
  the attachment subnets: per availability zone a /28 plus the /64 from the top of the block (room for four zones).
  Two placements may share a /26: the CIDR never leaves its VPC.
- It is never routed. The edge routers' IPv4 route-map from the transit gateway denies the whole pool in every
  network VRF, so it never enters the network nor becomes an EVPN Type-5 route; it is not allocated from the
  network, not in `status.subnets` and not in the IPAM. The only record is `status.realization.site.placeholderV4`.
  List the pool in `FABRIC_REALIZATION` `network.exclusiveRanges` so no tenant network can hold it.
- Without a pool an IPv6-only placement on AWS fails with `FamilyUnavailable` ("this site has no placeholder IPv4
  for IPv6-only VPCs").

**Workload subnets** are IPv6-only (`Ipv6Native`, a /64 per availability zone from the bottom of the block), with
resource-name hostnames (required on IPv6-only subnets) and their AAAA record on launch. The VPC has no IPv4 default
route. Instances reach the instance metadata service over IPv6 at `fd00:ec2::254` on Nitro instance types (enable
`HttpProtocolIpv6` on the instance); cloud-init works with it.

**Reaching IPv4 follows the source** (`status.ipv6.translation`, with its `path`):

| | Source `Network` | Source `Provider` |
|---|---|---|
| Translator | the site's translation (`translation.<site>`): its `pref64`, reached through the fabric's `::/0` | the NAT egress VPC (AWS realization `edge.egress.mode: nat-vpc`): its NAT gateway translates `64:ff9b::/96` |
| DNS64 | the site's DNS64 resolvers | the VPC resolver (`fd00:ec2::253`) with DNS64 on the workload subnets |
| Resolvers for the guests | a DHCP option set `shc-<namespace>-<name>` on the VPC with the translation's resolvers (`AmazonProvidedDNS` while translation doesn't apply); the VPC holds IPv6-only workload subnets only, so no dual-stack guest is left without a resolver | the VPC's default option set |
| IPv4 source on the internet | the site translator's addresses | the NAT gateway's Elastic IP (`translation.ipv4Source`) |
| Objects | the option set | DNS64 on the subnets; `64:ff9b::/96` to the transit gateway in the VPC; in the NAT egress VPC an Amazon /56, a /64 on each of its subnets, IPv6 on its attachment, `64:ff9b::/96` from the attachment subnet to the NAT gateway and the placement's block from the NAT gateway's subnet back to the transit gateway; a static `64:ff9b::/96` to the egress attachment in the network's transit gateway route table |

Translation needs `spec.egress.internet` (and, for source `Provider`, the realization's `translation.<site>.provider`
and a NAT egress VPC). Without it `status.ipv6.translation` is null and none of the `64:ff9b::/96` objects exist:
they are removed when the network turns internet access off, and come back when it turns it on. Source `Network`
needs no NAT egress VPC (nothing in the VPC sends IPv4); source `Provider` uses the same NAT egress VPC a dual-stack
placement uses for IPv4 internet access (the one of the placement's transit gateway route table), with no NAT gateway
of its own.

**Costs.** The placeholder costs nothing. Source `Network` adds nothing beyond a dual-stack placement and builds no
NAT egress VPC. Source `Provider` with translation pays for the NAT egress VPC: its NAT gateway's hourly charge and
per-GB processing, its Elastic IP and attachment, plus the transit gateway's per-GB charge for the translated
traffic.

**Refused:**
- An installation without `tenant.placeholderV4Pool`: `FamilyUnavailable` (above).
- Source `Provider` on a network with tenant prefixes: `PrefixInvalid`, as for dual-stack; with source `Network` such
  a placement gets no translation.
- Public IPv4 subnets for internet-facing load balancers: an IPv6-only placement has none.

**Teardown** adds, in reverse order: the VPC's `64:ff9b::/96` route with its other IPv6 routes, the DHCP option set
(the VPC goes back to the default set first) before the VPC's IPv6 block, and with the NAT egress VPC its
NAT64 routes and the static route.

## Cloud sites: Google Cloud placements

A Google Cloud environment's placement is one VPC in the environment's project with one regional subnet. With
`spec.ipv6` the subnet is dual-stack (IPv6-only without `spec.ipv4`: [Google Cloud: IPv6-only
placements](#google-cloud-ipv6-only-placements)), and its IPv6 range always comes from Google: source `Provider`
is the only one offered (`placement.ipv6.sources.CloudGCP`) and the default (`placement.ipv6.defaultSource.CloudGCP`).

| | Source `Provider` (Google's range) |
|---|---|
| Range | the /64 Google assigns to the subnet (external IPv6 access: Google's globally routed space) |
| Subnet | stack type `IPV4_IPV6`, IPv6 access type `EXTERNAL`; an IPv4-only subnet of the placement is changed in place, its IPv4 range stays |
| HA VPN gateway | dual-stack (`IPV4_IPV6`, IPv4 outer addresses; the tunnels and the edge routers' addresses stay IPv4). A gateway's stack type can't change after creation, so with the site's edge IPv6 on (the GCP realization's `edge.ipv6.enabled`) every new placement gateway is dual-stack, IPv4-only placements included, at the same price. A placement gateway that exists IPv4-only is deleted and created again when its placement asks for IPv6: the Cloud Router's interfaces and peers of its two tunnels go, then the tunnels and the gateway; the tunnels come back with new shared secrets, which reach the routers with the routers' VRF. Its BGP sessions are down meanwhile |
| Routing with the fabric | the Cloud Router's peers exchange IPv6 on their IPv4 sessions (`enableIpv6`); Google assigns each peer its IPv6 next-hop pair from `2600:2d00:0:2::/63`, and the routers take the pair from the placement job. Google never advertises an external IPv6 range by default, so each peer advertises in custom mode: all subnets plus the subnet's /64, set back on every run |
| Firewall | `<vpc>-fabric6`: IPv6 ingress, all protocols, from the network's IPv6 space (`HybridNetwork` `status.ipv6.prefixes`), the fabric's ICMPv6 sources (`core.icmpSource6`, so Packet Too Big from the fabric reaches the workloads) and the subnet's own /64; its sources are set back on every run. A rule holds one family, so the IPv4 rule stays as it was. Nothing from the internet: Google's implied deny-ingress rule covers IPv6, and the platform adds no inbound rule |
| Internet | Google's own path: the VPC's IPv6 default route (`::/0` to the default internet gateway), present only while the network's `spec.egress.internet` is on. It is deleted when the setting goes off and put back as `<vpc>-default6` when it comes on again. Never through the platform (the range is not in the fabric's egress filter); the IPv4 egress of the placement (`spec.egress`) is unchanged |
| `status.ipv6.internet` | `{state: ready, path: Provider}` while `spec.egress.internet` is on, otherwise none |

**What the fabric sees.** The edge routers learn the /64 from the Cloud Router and announce it to the site gateway as
an EVPN Type-5 route in the placement's VRF, like the IPv4 prefix. The /64 is recorded in the network's address space
as a provider-assigned subnet (the IPAM's `external` entries, action `ipam-external`): the placement's
`status.subnets` then carries it with `source: Provider`, the gateway's route check waits for it like for an
allocated prefix, and the network's placements in VM and OpenStack environments reach it through the fabric. The
routers announce the network's IPv6 space toward the VPC, never a default route, and never another cloud site's
provider-assigned range: two provider-source placements of one network on different cloud sites reach each other over
the internet, in both directions.

**What the tenant sees.** `status.subnets` has a family-6 row `{family: 6, prefix, source: Provider}` (no gateway:
Google's subnet router answers); `status.ipv6` has `addressing` (`DHCPv6`) and `internet` with its `path`;
`status.realization.site.ipv6` records `source`, `subnet`, `internet` and the IPv6 next hops per tunnel. A workload
uses the range through a dual-stack interface with an external IPv6 address (Google hands each interface a /96 of the
/64); its IPv4 internet access follows `spec.egress` as before. Google's metadata server answers on IPv6 only to
IPv6-only instances, and Cloud DNS's DNS64 does not apply to dual-stack instances. A re-run creates nothing.

**Costs.** A dual-stack HA VPN gateway costs the same as an IPv4-only one (per tunnel-hour, two tunnels per
placement); external IPv6 addresses on instances have no charge; internet traffic is billed at Google's data
transfer rates. The edge routers are shared with IPv4.

**Reverse DNS.** None from the platform: the range is Google's.

**Refused** (before anything is allocated or built), all with `FamilyUnavailable`:
- source `Network`: "this site offers IPv6 only from the cloud provider; use source Provider". A Google subnet's
  IPv6 is Google's own space, a Google ULA, or brought-your-own space that Google either announces or keeps as a
  separate prefix, so none of them can be a slice of the network's IPv6 space.
- a network with tenant prefixes (mode `Tenant`), with or without `source`: "this site can't carry tenant-provided
  IPv6 prefixes". The mode is checked before the default source is applied.
- IPv6-only placements on a site that doesn't offer them: "IPv6-only placements are not offered on this site"
  (`placement.ipv6OnlyBySite.<site>` off, or the site's `capabilities.ipv6Only` false). Where they are offered, see
  [Google Cloud: IPv6-only placements](#google-cloud-ipv6-only-placements).
- `prefixLength` or `prefix` with source `Provider`: refused at admission. Bringing your own IPv6 range (BYOIP): not
  offered (no field).
- OpenShift clusters stay IPv4: a cluster on a dual-stack placement takes the placement's IPv4 subnet as its machine
  network.

**Teardown** goes in reverse: the tunnels, the Cloud Router, the HA VPN gateway, the firewall rules (the IPv6 rule
too), the subnet with its /64, the `<vpc>-default6` route if the platform put it back, then the VPC (Google's own
default routes go with it) and the routers' VRF. The /64 leaves the network's address space with the placement's
subnets.

### Google Cloud: IPv6-only placements

A placement without `ipv4` on a site with `placement.ipv6OnlyBySite.<site>: true` (and the site's
`capabilities.ipv6Only`) gets a VPC with no IPv4 range at all:

| | IPv6-only placement |
|---|---|
| Subnet | stack type `IPV6_ONLY`, IPv6 access type `EXTERNAL` (the /64 Google assigns, external access); no IPv4 range, no secondary ranges. A subnet's stack type can't change in either direction, so a placement is IPv6-only or dual-stack for its life (`spec.ipv4` can't be added or removed either) |
| HA VPN, routing, firewall | as for a dual-stack placement: the dual-stack HA VPN gateway, the Cloud Router's peers exchanging IPv6 on their IPv4 sessions (the link-local /30 per tunnel carries the session, not workload traffic), the custom advertisement of the /64, `<vpc>-fabric6`. The IPv4 firewall rules stay; they admit nothing without IPv4 workloads |
| Internet | Google's `::/0`, present only while the network's `spec.egress.internet` is on, as for a dual-stack placement. Google's IPv4 default route of the VPC stays whatever `spec.egress` says: no workload sends IPv4, and Cloud NAT forwards translated traffic by the VPC's IPv4 routes |
| DNS64 | while translation applies: a Cloud DNS server policy `<vpc>-dns64` bound to the VPC with DNS64 for all queries (`dns64Config.scope.allQueries`); IPv6-only instances then get AAAA answers inside `64:ff9b::/96` for IPv4-only names. Google applies DNS64 to IPv6-only instances only |
| NAT64 | while translation applies: one Cloud NAT `<vpc>-nat64` on the placement's Cloud Router, translating `64:ff9b::/96` for all IPv6-only subnet ranges (`sourceSubnetworkIpRangesToNat64: ALL_IPV6_SUBNETWORKS`), with addresses Google allocates (`natIpAllocateOption: AUTO_ONLY`) and no IPv4 source ranges. If Google's IPv4 default route is missing, `<vpc>-default4` is added for it |
| Resolver and metadata | the metadata server: Google gives an IPv6-only instance a link-local IPv4 address and `169.254.169.254` as its DNS resolver (which applies DNS64); metadata also answers over IPv6 at `fd20:ce::254` (on IPv6-only instances only) |
| `status.ipv6.translation` | `{path: Provider, pref64: "64:ff9b::/96", resolvers: ["169.254.169.254"], ipv4Source}`; `ipv4Source` is the Cloud NAT's address once Google has allocated one (it allocates on demand, so it appears on a run after the first instance used NAT64) |

**When translation applies** (the provider's path, [Translation](#translation-translation)): the network's IPv6
comes from the platform, `spec.egress.internet` is on, and the site's `translation.<site>` entry has
`provider: true`. Nothing of the fabric's IPv6 egress or translator is involved. When it stops applying (internet
access off, the entry removed) the next run removes the NAT and detaches and deletes the policy, and
`status.ipv6.translation` is cleared; the placement keeps its IPv6 and its routes to the network's other sites.

**Instances.** Google offers IPv6-only instances with Debian and Ubuntu images only; other images (CentOS, RHEL)
need a dual-stack placement. An instance on the subnet takes `stackType: IPV6_ONLY` and an external IPv6 access
config. IPv4-only names are reached by name through DNS64 and NAT64 with no software in the guest; an IPv4 literal
needs a CLAT in the guest, with `64:ff9b::/96` as its translation prefix.

**What the tenant sees.** `status.subnets` has only the family-6 row; `status.realization.site` names no IPv4
subnet, and `status.realization.site.ipv6` adds `only: true` and, with translation, the NAT's and the policy's
names. The site side's `place-status` reports the NAT (with its addresses) and the policy and whether each is in step.

**Costs.** As for a dual-stack placement, plus Cloud NAT while translation applies: an hourly charge per instance
that uses it and a per-GB charge for translated traffic (Google's Cloud NAT rates). The DNS server policy has no
charge.

**Teardown.** As for a dual-stack placement; the Cloud NAT goes with the Cloud Router, and the DNS policy is
detached and deleted (and `<vpc>-default4` removed, if the platform added it) before the subnet and the VPC.

## Translation (`translation`)

```yaml
translation: {}   # <site>: {pref64: <the site's translation /96>, dns64Resolvers: [<IPv6 resolver>, ...],
                  #          provider: <true: the cloud provider's own DNS64 and NAT64 for source Provider>}
```
- **`pref64`:** the IPv6 /96 into which the translator (NAT64) the site's IPv6-only workloads use embeds IPv4
  addresses. The platform doesn't run the translator; it reaches it through the IPv6 egress (`egress.ipv6`). List
  the prefix in `allowDestinations6` when it lies inside `denyDestinations6`, and in `network.exclusiveRanges` so no
  tenant prefix overlaps it.
- **`dns64Resolvers`:** IPv6 resolvers that answer IPv4-only names with addresses inside `pref64` (DNS64), in the
  order the workloads should use them.
- **`provider`:** on a cloud site, `true` offers the provider's own translation to placements whose IPv6 comes from
  the provider (source `Provider`); `pref64` and `dns64Resolvers` then aren't needed for them (they serve source
  `Network` only). A Google Cloud site's entry is `{provider: true}`.
- An entry turns the site's `capabilities.ipv6Translation` on (with the site's IPv6, see
  [Site capabilities](#site-capabilities)); a site without an entry offers no translation. Keep the entry at the top
  level of `FABRIC_REALIZATION`, like the `placement` switches. Translation is useful only where the site offers
  IPv6-only placements (`placement.ipv6OnlyBySite`).

**Who gets it.** Translation follows from the network and the placement; there is no tenant switch. Only a placement
with no IPv4 subnet, whose network's IPv6 comes from the platform (mode `Platform`) and asks for internet access
(`spec.egress.internet: true`), gets it, and its path follows the placement's IPv6 source:

| Source | Path | Also needs | `status.ipv6.translation` |
|---|---|---|---|
| `Network` (every environment type) | `Fabric`: the site's translator, reached over the fabric's IPv6 egress | the fabric offers IPv6 egress (`egress.ipv6.enabled`), the site reports `capabilities.ipv6Translation` and its entry has a `pref64` | `{path: Fabric, pref64, resolvers}` from the site's entry |
| `Provider` (cloud environments) | `Provider`: the cloud's own DNS64 and NAT64, built by the placement's site side | the site's entry has `provider: true` (the fabric's IPv6 egress is not involved) | `{path: Provider, pref64, resolvers[, ipv4Source]}` as the site side reports them; `ipv4Source` is the provider translator's IPv4 address once the provider reports it |

Otherwise the field is cleared, so a placement that stops qualifying (the entry removed, IPv6 egress turned off,
internet access off) loses it on its next run, and on the provider's path its site side removes the provider's
translation objects. Translation needs `egress.internet` on: on the fabric's path the translator is reached over the
network's IPv6 internet path, on the provider's path the provider's translator is an internet gateway; a network
without internet access has no translation and no provider translation objects. A provider-path placement's first
run builds the translation before its status shows it (the field keeps its previous value until the site side has
reported). Dual-stack placements never get the DNS64 resolvers: their workloads reach IPv4 natively and would
otherwise send IPv4-only names through the translator.

The network reports the translation in `status.egress.ipv4Translation` (only for a network without IPv4 that asks
for internet access), covering both paths: the fabric's path is usable while the fabric offers IPv6 egress and a
site has a `translation` entry with a `pref64`; the provider's path is offered while a site has an entry with
`provider: true`, and needs nothing of the network's own (each placement's site side builds it).

| Network | `ipv4Translation` |
|---|---|
| mode `Tenant` | `{state: unavailable, reason: AddressChoice}` (neither path serves tenant-provided prefixes) |
| mode `Platform`, neither path offered | `{state: unavailable, reason: NotOffered}` |
| mode `Platform`, the fabric's path only | the state of the network's IPv6 egress row (`pending`, then `ready`) |
| mode `Platform`, the provider's path offered | `ready`; the message says which placements use which path, and whether placements with space from the network have translation (the fabric's path `ready`), will have it (`pending`) or have none (not offered) |

The row says what the network's placements can use; each IPv6-only placement's own `status.ipv6.translation` names
the path it uses, its prefix and resolvers (a provider-path placement's block appears once its site side has built
the translation).

**The resolvers through the egress filter.** The IPv6 egress filter of every border gateway node admits the DNS64
resolvers of every site's entry on port 53 (TCP and UDP), in addition to `egress.ipv6.resolverDestinations6`: the
fabric job derives the list (single addresses, sorted, each once), so the resolvers are not configured twice. The
rule comes before `denyDestinations6`, so resolvers inside a denied range (a unique local address inside `fc00::/7`,
for example) are reachable on port 53 and nothing else. The private IPv4 ranges of `egress.denyDestinations`, in
their forms embedded in every `pref64`, are denied before the allow list, so the translator never re-opens them.
Run the fabric job (Retry now on the HybridFabric) after changing an entry.

**What each site does.**
- **OpenStack sites (CloudOSO):** an IPv6-only placement with translation gets the DNS64 resolvers first in its IPv6
  subnet's `dns_nameservers` (then the site's IPv6 resolvers from `placement.resolversBySite`, if any), so DHCPv6
  and the config drive carry them; a placement that doesn't qualify keeps the site's resolvers only. A subnet whose
  list differs is updated on the next run of the placement job.
- **OpenShift Virtualization sites (CloudVirt):** the platform changes nothing on the site. The resolvers reach the
  guests through the placement's status and the console's cloud-init snippet (the placement page builds `networkData`
  with `dhcp4: false` and the resolvers as `nameservers`, and clatd user data with the placement's `pref64` as
  `plat-prefix`), which the tenant copies into the VM. Cluster-internal names (services, other VMs by name) reach a
  guest over DHCPv6 only when the cluster's DNS service (`dns-default` in `openshift-dns`) is dual-stack; on a
  cluster where it has only an IPv4 address, DHCPv6 hands out no cluster resolver and an IPv6-only guest resolves
  through the DNS64 resolvers alone.

- **Google Cloud sites (CloudGCP):** source `Provider` only, so the provider's path: the VPC's DNS64 server policy and
  the Cloud NAT for NAT64 ([Google Cloud: IPv6-only placements](#google-cloud-ipv6-only-placements)); the resolver is
  the metadata server, which IPv6-only instances use without configuration.

OpenShift clusters (PlatformOpenshift) are refused on a network whose placement has no IPv4 subnet: the platform
reaches clusters over IPv4.

**Rollback.** Set `placement.ipv6OnlyBySite.<site>` to `false`: new IPv6-only placements are refused, existing ones
keep working inside the network. Removing the site's `translation` entry clears `status.ipv6.translation` on the
placements' next run and the resolvers from the egress filter on the fabric's next run.

### Translation canary

```yaml
canaries:
  ipv6Translation:
    network: ""        # "<namespace>/<name>" of the platform team's IPv6-only network; "" = no canary
    workload: ""       # the probe's name on every placement of that network (VM or server)
    port: 9464         # the probe's metrics port
    alertMinutes: 5    # a site failed or stale this long is reported as a Warning event on the fabric
```
The translation canary is a probe VM the platform team runs on every site that offers IPv6-only placements with
translation, on an IPv6-only network of its own tenant (IPv6 from the platform, internet access on). Every minute it
checks what a tenant's IPv6-only guest depends on: its global address, every translation resolver, that
`ipv4only.arpa` resolves inside the site's translation prefix, an IPv4-only name by name and an IPv4 literal through
its CLAT, a native IPv6 name, a 1400-byte path with "don't fragment", and that a denied IPv4 destination embedded in
the translation prefix stays refused. It serves the results in Prometheus text format at
`http://[<address>]:<port>/metrics` (`shc_ipv6_canary_check{check="<name>"} 0|1`,
`shc_ipv6_canary_run_timestamp_seconds`). Deploy it from [samples/platform/ipv6-canary](../../samples/platform/ipv6-canary/README.md),
then set the realization keys. With `network` empty nothing below is written.

**The site job** finds the probe: the canary network's placement on the site (a NetworkPlacement in the network's
namespace whose `status.site` is the site, of the site type's backend kind), then on OpenShift Virtualization the
VirtualMachineInstance named `workload` in the placement's site namespaces (`status.namespaces`; read with the site's
fabric credential), on OpenStack the server named `workload` in the environment's project (read with the site
account), and takes the probe's first global IPv6 address (on OpenStack: on the placement's network). The probe's
address is never read from labels or annotations on tenant objects. It records on the CloudInfrastructure:

```yaml
status:
  realization:
    ipv6Canary:
      placement: <namespace>/<name>   # the canary network's placement on this site
      vrf: t<vni>                      # the placement's VRF on the site's gateway
      address: <the probe's global IPv6 address>
      workload: <namespace>/<name> of the VirtualMachineInstance, or the server's id
      # instead of address: error: <why the probe or its address was not found>
```
No placement of the canary network on the site: no `ipv6Canary`. A lookup that fails never fails the site job; it is
recorded as `error`.

**The fabric job**, after the nodes are configured, reads the canary of every site of the fabric (the sites of its
gateways) that reports one, on one border gateway node (the first that answers): from the site's gateway namespace in
the placement's VRF, `ip netns exec gw-<gateway> ip vrf exec <vrf> curl -6 http://[<address>]:<port>/metrics`, with
the VRF's own global IPv6 address as source (the ICMPv6 source of the dual-stack core, `core.icmpSource6`; the probe's
replies must route back to it). It records on the HybridFabric:

```yaml
status:
  realization:
    translation:
      canaries:
        <site>:
          placement: <namespace>/<name>
          address: <the probe's address>
          at: <when the probe's last run finished>
          checks: {address: 1, resolver: 1, pref64: 1, name4: 1, literal4: 1, name6: 1, mtu: 1, deny: 1}
          stale: false        # true: the last run is older than three minutes, or the probe was not reachable
          healthy: true       # reachable, not stale, every check 1
          error: ""           # why not healthy, when that is not a failed check
          failedSince: ""     # since when it has not been healthy (kept across runs)
```
The condition `IPv6Translation` is `True` (`Ready`) while every site's canary is healthy and `False`
(`CanaryFailed`) naming the failing sites, their failed checks and since when; it is absent while no canary is
configured or no site reports one (the status library never removes a condition: after the canary is switched off the
last one stays until the fabric is recreated). Once a site has been failed or stale for `alertMinutes`, every fabric
run while it lasts adds a Warning event `IPv6TranslationCanaryFailed` on the HybridFabric, as for the IPv6 egress.
A canary that could not be read at all (no node answered) reads `stale` with the reason in `error`; the fabric job
itself never fails because of the canary.

**Testing the alert:** on one site, replace the probe with a copy whose user data names a resolver address where no
resolver answers (same probe name). Its `resolver` check reads 0 within a minute, the condition turns `False` on the
next fabric run, and the event follows after `alertMinutes`. Put the original user data back to clear it.

## IPv6 internet egress (`egress.ipv6`)

```yaml
egress:
  ipv6:
    enabled: false            # routed IPv6 egress for platform-assigned space; off: IPv6 internet reads NotOffered
    routedPrefixes: []        # the platform's globally routed IPv6 space (the platform address pools lie inside it)
    asn: 0                    # the egress VRF's own AS toward the upstream routers
    upstream: []              # [{address, asn}]: the upstream routers on the hand-off network (one session each per node)
    nodes: {}                 # per node: {address: <address/length on the hand-off network>}
    wanMtu: 1420              # IPv6 MTU of the hand-off device and the TCP MSS clamp (wanMtu - 60)
    maxPrefixesUpstream: 10   # prefixes accepted from each upstream (the default plus the external site prefixes)
    maxPrefixesNetwork: 1000  # prefixes accepted from each network's VRF
    standbyCommunity: 998     # the standby node's default route carries <domainAsn>:998 ...
    standbyLocalPref: 50      # ... and reaches the sites with this local preference (they prefer the primary node)
    standbyPrepend: 2         # ... and the standby node prepends its AS this often toward the upstream
    sessionDownAlertMinutes: 5  # a node without an upstream session for this long is reported as a fabric event
    denyDestinations6: [...]  # the IANA special-purpose ranges; an installation adds its own address space
    allowDestinations6: []    # reachable although inside denyDestinations6
    resolverDestinations6: [] # resolvers reachable on port 53 only (the translation entries' DNS64 resolvers are added)
    externalSitePrefixes: {}  # per network: routed sources that legitimately arrive from upstream
```
IPv6 egress is routed, not translated: a network's platform block leaves unchanged through a stateful filter on the
border gateway nodes that admits only replies to connections opened from inside. Tenant-provided prefixes never get
an internet path. While `enabled` is false every network in mode `Platform` reports
`{family: 6, state: unavailable, reason: NotOffered}` in `status.egress.families`, and `status.egress.internet`
keeps reporting the IPv4 state of networks with IPv4.

With `enabled` the fabric job gives the egress VRF of every border gateway node its address on the hand-off network
and one eBGP session (AS `asn`, timers 10/30, no BFD, no password) to each upstream router: in, the default route
only; out, the placement prefixes of the networks with internet egress and IPv6 from the platform (prefixes inside
`routedPrefixes`, longer than it and at most /64; never an aggregate). Per such network the root VRF `e<vpnTarget>`
gains an unnumbered IPv6 session over its veth into the egress VRF: it takes the default route (which therefore
reaches the network's VPN, and the sites, only while an upstream sends it) and announces exactly its placement
prefixes, each originated as an aggregate of the routes the sites send. The whole of `routedPrefixes` is discarded
(`reject`) in the egress VRF and in every network VRF, so space the fabric does not route never loops.

Each network's IPv6 internet traffic leaves and returns through one primary node, the node at position (egress index
mod number of nodes): the standby node marks its default route with `<domainAsn>:<standbyCommunity>` (the gateways
hand it to the sites with `standbyLocalPref`) and prepends its AS `standbyPrepend` times toward the upstream. When
the primary's upstream sessions fail, the sites' default moves to the standby and the upstream's return path follows
the prefixes the standby still announces; flows where the tenant sends next recover, receive-only flows reset.

The filter is the nftables table `inet shc_egress6` on every node (IPv6 rules only; the IPv4 table stays as it is):
outbound from a network only from its platform block (and the node's ICMPv6 source), not to the translator-embedded
forms of `egress.denyDestinations` (from `translation.<site>.pref64`), then `allowDestinations6` and the resolvers on
port 53 (`resolverDestinations6` and every `translation.<site>.dns64Resolvers`), then not to `denyDestinations6`; a TCP
MSS clamp at `wanMtu - 60` both ways; inbound only neighbour discovery, ICMPv6 errors and replies to connections opened
from inside; a source inside `routedPrefixes` arriving from upstream is dropped (`externalSitePrefixes` excepted). The
table is loaded before FRR takes the sessions' configuration, and the timer unit `shc-egress6-guard` shuts the upstream
sessions whenever the table is missing (fail closed). The hand-off device's IPv6 MTU is `wanMtu`, so the node itself
sends Packet Too Big for larger packets bound for the internet.

Status: `HybridFabric status.realization.egress.ipv6` holds per node the hand-off address, the upstream sessions'
states and whether the default route is held; `status.realization.egress.networks[]` says per network whether its
IPv6 egress is realized (`ipv6`), the prefixes announced (`prefixes6`) and its primary node. A network with IPv6
egress reads `{family: 6, state: ready, mode: Routed}`, or `reason: Degraded` while some node's upstream sessions
are all down, and `state: unavailable, reason: UpstreamDown` while no node has an upstream session (translation to
IPv4 on the fabric's path then reads `UpstreamDown` too); a node down for `sessionDownAlertMinutes` is reported as a
Warning event on the HybridFabric. A placement with IPv6 from the network copies the network's row into
`status.ipv6.internet` (null when the network has no row); the network wakes those placements whenever its row
changes.

The gateways announce covering routes into every site: the network's own IPv6 prefixes (its platform block, or the
tenant's prefixes) and `routedPrefixes`, each backed by an unreachable route in the VRF table. A destination the
fabric does not route (an unallocated prefix, another tenant's space) therefore fails fast at the gateway with an
ICMPv6 unreachable and never leaves for the internet; the placement prefixes and the default route are longer or
shorter than these and take precedence as usual. While a network has no IPv6 egress its gateways keep announcing
`::/0` themselves (unreachable), so internet-bound IPv6 fails fast and guests fall back to IPv4.

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

## Reverse DNS (`dns.reverse6`)

```yaml
dns:
  reverse6:
    zones: []                # reverse zones the platform owns, e.g. 8.b.d.0.1.0.0.2.ip6.arpa for 2001:db8::/32
    gatewayLabel: gateways   # the label gateway names are published under
    ttl: 3600                # TTL of the platform's AAAA and PTR records
    staleMinutes: 30         # how old the tenant DNS controller's last success on a site may be
```

With no zone (the default) the platform writes no reverse DNS. List a zone only for space the platform assigns from
its own pools and whose reverse delegation points at the Designate pool of the global tenant zone; the delegation of
the zone itself (the NS records in the parent reverse zone) is the address holder's step and comes after the zone
exists, otherwise it is a lame delegation.

**Zones.** The CloudInfrastructure job of the site that hosts the global tenant zone (`dns.mode: native`, a usable
DNSConfig, and the parent zone it ensures is the DNSConfig's zone) creates every listed zone next to the global zone:
the same DNS project, the site account (the cloud admin; a cloud's DNS service may let only an administrator create a
zone under a delegated reverse block), the zone attributes of the site's `dns.zoneAttributes` (pool scheduling, e.g.
`scope: public`), email `hostmaster@<global domain>`. It records them in
`status.realization.dns.reverseZones` (`[{name, id, nameservers}]`, or `{name, error}` for a zone it could not
ensure); a zone error never fails the site. Other sites write nothing; a site that no longer hosts the zones loses
the record. Removing a zone from the list does not delete it. When the DNS service refuses the name to the site
account (a blacklist under the delegated reverse block answers "Blacklisted zone name"), an administrator creates
the zone with the same attributes and hands it to the DNS project by a zone ownership transfer; the site job then
finds it there on its next run and records it.

**Who writes which record.** Every name is forward-confirmed: an AAAA in a forward zone and the PTR of the address in
the reverse zone that covers it (the longest one), both written with the DNSConfig's DNS-only account (a member of the
DNS project) and both carrying the ownership marker `shc:<owner>` in the recordset description. A recordset of the same
name and type with another marker is never changed or deleted ("held by another owner").

| Address | Name (AAAA in) | Owner | Written by |
|---|---|---|---|
| A placement's IPv6 gateway (`status.subnets[family=6].gateway`: the first host on OpenShift Virtualization, the subnet's gateway from Neutron on OpenStack) | `<network>.<gatewayLabel>.<environment zone>` (the environment's zone, `status.dns.zone`) | `NetworkPlacement/<namespace>/<name>` | the placement job, recorded in `status.realization.reverseDns` |
| A border gateway node's ICMPv6 source in a gateway namespace (`core.icmpSource6`) | `<node>.<gateway>.<gatewayLabel>.<global domain>` (the global zone) | `CloudGateway/<name>` | the gateway job, recorded in `status.realization.reverseDns` |
| An AWS or Google Cloud edge router's ICMPv6 source (`core.icmpSource6`, while the realization's `edge.ipv6` is on) | `<router>.<gateway>.<gatewayLabel>.<global domain>` (the global zone; `<router>` is the router instance's name) | `CloudGateway/<name>` | the gateway job, with its nodes' names |
| A tenant VM on OpenShift Virtualization that publishes an AAAA through the tenant DNS controller | the tenant's own name in its environment zone | the controller's marker | the tenant DNS controller, only for addresses inside the namespace's placement prefixes |

The placement job also writes the annotation `hybridsovereign.redhat/ipv6-prefixes` (the placement's IPv6 subnets,
comma-separated) on every OpenShift Virtualization namespace the placement serves; the tenant DNS controller trusts
only that annotation (tenants can't change it, as with `hybridsovereign.redhat/dns-zone`). Node labels and network
names are lowercased, with characters outside `a-z0-9-` replaced by `-`.

**Notes, not failures.** An environment without a zone, an environment zone held by another site's DNS service, an
address no listed zone covers and every API error end in the job's status message (`reverse DNS: …`); the placement
and the gateway still become ready. A name the job published before and no longer wants (the IPv6 subnet went away, a
node left) loses its PTR, then its AAAA, on the next run.

**Teardown order.** A placement removes its PTR, then its AAAA, after the site side and before its subnets go back to
the network's address space, so the next holder of the prefix never inherits a name; while a removal fails (with a
usable DNSConfig) the deletion waits and retries. It also removes the namespace annotation where the value is still
its own. A gateway removes its names once its instance is off the nodes; a failure is noted in the final message.

**Not covered.** Space from a cloud provider (AWS, GCP) and tenant-provided prefixes have their reverse DNS with the
provider or the tenant: no listed zone covers them, so they get no PTR. OpenStack tenant VMs get no PTR from the
platform (Neutron's own DNS integration is the cloud's). Per-network reverse sub-zones shared with tenants are not
offered.

## Attachment circuits

```yaml
attachments:
  reservedAsns: []   # AS numbers a tenant's edge router may not use (the platform's own, e.g. its egress AS)
```
A circuit whose peer AS is listed fails with `PeerAsnReserved`. IPv6 entries in a circuit's accept list stay refused at
admission.

## Tenant reverse names

The tenant DNS controller on an OpenShift Virtualization site (chart `shc-cluster-base`, `tenantDns`) publishes the
names tenants ask for in their environment zone. For an AAAA it publishes, it also writes the reverse name, so that
every tenant PTR is forward-confirmed:

- **Which addresses:** those inside the namespace's placement prefixes. The platform writes them on every
  namespace a placement serves, in the annotation `hybridsovereign.redhat/ipv6-prefixes` (comma-separated IPv6
  prefixes), the same trust model as the zone annotation `hybridsovereign.redhat/dns-zone`: tenants can't change
  namespaces. Entries that are not IPv6 prefixes are skipped (logged once). A namespace without the annotation gets
  no reverse names.
- **Which zone:** the longest reverse zone (`*.ip6.arpa`) of the controller's DNS project that contains the
  address's reverse name. The platform creates those zones (`dns.reverse6.zones`); the controller only finds them
  in the zone listing it already reads on every round.
- **The record:** a PTR at the address's reverse name whose target is the AAAA's name, with the AAAA's ownership
  description (`shc-tenant-dns cluster=<cluster> owner=<namespace>/<Kind>/<name>`). One owner per reverse name: when
  two objects publish the same address, the older object's name wins, as for forward names.
- **Not written:** an address outside the prefixes ("no PTR for <address>: outside the placement prefixes"), one no
  reverse zone covers ("no PTR for <address>: no platform reverse zone"), or a reverse name another owner holds.
  The object's `hybridsovereign.redhat/dns-status` says so, with one Event when that status changes; the AAAA is
  still published. A PTR is written only after its AAAA is published. Tenants can't request PTRs themselves (the
  type is never allowed in their objects).
- **Removed:** with its AAAA (the object, its name or the address goes, or the address leaves the namespace's
  prefixes), and in the sweep of the controller's own records: a PTR whose owner object is gone or whose AAAA it no
  longer publishes. Records of other clusters and records without the controller's marker are never touched.

**Health.** After every round the controller writes the ConfigMap `tenant-dns-status` in its own namespace:

```yaml
data:
  lastSuccess: "2026-01-01T10:00:00Z"   # the last successful round (RFC 3339, UTC); "" before the first
  lastError: "HttpException: ..."       # the last failure (kept after a later success)
  lastErrorAt: "2026-01-01T09:55:00Z"
  refused: "0"                          # objects with a refusal in the last successful round
  ptrs: "3"                             # PTRs the controller holds after it
```
A failed round also adds a Warning Event `DNSReconcileFailed` on that ConfigMap, once per distinct error (again after
a success). The pod's liveness file still only tells that the loop runs.

**The site job** of an OpenShift site reads that ConfigMap with the site probe's credential (it may read that one
ConfigMap name) and records it on the CloudInfrastructure, with the condition `TenantDns`:

```yaml
dns:
  tenantDnsNamespace: shc-tenant-dns   # the tenant DNS controller's namespace on the sites (the chart's default)
  reverse6:
    staleMinutes: 30                   # a last success older than this turns TenantDns False
```
```yaml
status:
  realization:
    tenantDns: {lastSuccess, lastError, lastErrorAt, refused, ptrs}   # null: no ConfigMap on the site
  conditions:
    - type: TenantDns
      status: "True"                   # reason Ready
      # "False", reason DNSReconcileFailed: the last error is newer than the last success, no success yet, or the
      #   last success is older than staleMinutes
      # "Unknown", reason NotReported: the record went after one was reported, or it could not be read
```
The condition does not change the site's phase: names keep resolving while the controller is down, only changes
wait. A site that never reported a record gets no condition. The read never fails the site job.

## Cloud sites: AWS

An AWS site carries IPv6 in two halves: the platform's edge routers and the transit gateway in the hub account (this
section), and each placement's VPC in the tenant account. Neither changes the underlay: the routers' WireGuard
tunnels to the border gateways, their GRE tunnels to the transit gateway and the BGP sessions stay IPv4.

### AWS edge

The edge settings live in the installation's `AWS_REALIZATION` (`edge`), next to the IPv4 ones; the generic defaults
(`common/tasks/aws_realization.yml`) leave IPv6 off:

```yaml
edge:
  insideCidrPool: 169.254.100.0/24   # IPv4: a /29 per Connect peer (AWS's reserved /29s are stepped around)
  ipv6:
    enabled: false                   # IPv6 on the edge routers and their Connect peers
    insideCidrPool6: ""              # a ULA /64 from fd00::/8: a /125 per Connect peer
```
- **`insideCidrPool6`:** one per transit gateway. Its ledger is the /125s the region's Connect peers already hold;
  AWS reserves no IPv6 inside range, so nothing else is stepped around. Keep it out of tenant space: list it in
  `FABRIC_REALIZATION` `network.exclusiveRanges` (a pool inside `siteReserved.CloudAWS`, such as `fd00::/16`, is
  refused to tenants on AWS already). A pool that is not an IPv6 network inside `fd00::/8` stops the gateway job.
- **The site switches stay separate.** `edge.ipv6.enabled` prepares the routers and the peers; placements are offered
  IPv6 only once the site's `placement.ipFamiliesBySite` holds 6 ([Site capabilities](#site-capabilities)). Turn the
  edge on first.
- **Connect peers get a /125 at creation, and existing ones are re-created once.** The transit gateway has no IPv6
  BGP peering: it negotiates IPv6 unicast on a peer's IPv4 sessions only when the peer has an IPv6 inside /125, and a
  peer can't be modified. With the edge on, every new peer is created with its /29 and a /125 from the pool; a peer
  that exists without one (created before IPv6) is deleted and created again with the same /29 and a new /125 on the
  next run that brings its network up. Its BGP sessions are down for about 9 minutes, so do it in a window. The
  re-created peer is tagged `shc-ipv6-recreated` and is never re-created a second time; if it still has no /125 the
  job says so and the network stays IPv4 on that router. The router takes the /125's first address on its GRE
  device, the transit gateway the second and third (its next hops).
- **Routers created before IPv6 need a `replace`** (`shc_aws_edge.py replace`: new instances on the same addresses
  and Elastic IPs; the gateway's next run hands them the gateway nodes' keys): the agent that renders the IPv6 side
  ships with the instance.

**What the routers announce, per network VRF.** The gateway job hands the routers each placement's IPv6 settings
(`shc_aws_edge.py vrfs6`): the placement's family-6 subnets (`prefixes6`), the network's IPv6 space
(`HybridNetwork` `status.ipv6.prefixes`), the subnet's source and whether the network gets the fabric's IPv6 internet
(mode `Platform` with `spec.egress.internet`). FRR runs IPv6 unicast on the IPv4 sessions with a route-map per
family (FRR applies RFC 8212 to eBGP even with the traditional defaults):

| Direction | Source `Network` | Source `Provider` |
|---|---|---|
| From the transit gateway | the placement's prefixes only (the VPC's IPv6 blocks); never a default route | the same (the Amazon /56) |
| To the transit gateway | the network's other IPv6 space learned from the fabric; `::/0` only with internet (the fabric's IPv6 egress, from the network's own addresses) | the network's other IPv6 space only; the VPC's own egress-only internet gateway carries its default route |
| To the site's gateway (EVPN Type-5) | the placement's prefixes only | the same: the provider's prefix enters the fabric for traffic between sites |

The network's IPv6 space never holds another cloud's provider-assigned prefix, so a router never announces one into
its own cloud: two provider-source placements of one network on different clouds talk over the internet, both ways.
A network with tenant prefixes (mode `Tenant`) gets no `::/0` toward the transit gateway in any case. The gateway
accepts the routers' Type-5 routes inside the placements' prefixes only (its site-import filter), and the placement's
readiness checks those routes at the gateway per family, as on every site; a placement whose gateway took it up before
its IPv6 subnet was known asks the gateway again.

**The placeholder IPv4.** With the AWS realization's `tenant.placeholderV4Pool` set, the gateway job writes it to the
routers' configuration, and every network VRF's IPv4 route-map from the transit gateway denies the pool and anything
inside it ahead of its permit (prefix list `<vrf>-placeholder4`), so an IPv6-only VPC's IPv4 CIDR never enters the
VRF ([AWS: IPv6-only placements](#aws-ipv6-only-placements)). Without a pool the routers' configuration and FRR
render are as before.

**The transit gateway's IPv4 default.** Every network VRF's IPv4 route-map from the transit gateway also denies
`0.0.0.0/0` (prefix list `<vrf>-default4`). The transit gateway holds that default while the network's NAT egress VPC
exists (for the VPCs behind it); it is not a route for the network's other sites and never becomes a Type-5 route.
It also comes and goes with the network's internet access, and the site gateway (FRR 8.5) drops a VTEP's router MAC
when that VTEP's last IPv4 Type-5 route is withdrawn, even while IPv6 Type-5 routes still use it: an IPv6-only VPC
would then lose its path from the fabric. Routers created before this rule need a `replace` (the agent renders it).
If a site gateway ever shows a router's IPv6 Type-5 routes without a router MAC (`show evpn rmac vni <vni>` empty,
no `extern_learn` entry for it in `bridge fdb show dev vx<vni>`), resetting that router's EVPN session on the
gateway re-learns them.

**ICMPv6 source.** Each router gets a /128 on `lo<vni>` in every IPv6 VRF, from the fabric's `core.icmpSource6`: the
same formula as the border gateway nodes (network address + (node ordinal << 16) + the gateway's provider-edge
index), at node ordinal 100 + the router's position by name (100 for the first router, 101 for the second). The
nodes use their position in `borderGateway.nodes` from 1, so the two never meet while a fabric has fewer than 100
border gateway nodes. The routers install their fabric-side routes with that address as source, so Packet Too Big
and Time Exceeded toward the fabric come from it; toward the VPC they come from the GRE device's /125 address, which
the transit gateway delivers. Without the IPv6 core or `core.icmpSource6` the routers get no /128. Each VRF table
also gets an unreachable IPv6 default, so a destination nothing routes fails at once. The routers' VRFs keep the site
leg's MTU (1370 behind the WireGuard tunnels) for both families.

**State.** `shc_aws_edge.py status` lists per VRF its IPv6 settings and, under `ipv6`, per router the sessions that
negotiated IPv6 (`sessions6`, for example `2/2`), the IPv6 routes by protocol, whether a default route is present and
the placement prefixes not learned from the transit gateway yet (`missing6`). The routers' own reports carry `bgp6`
(per session: `af6` negotiated or `NoNeg`, the prefix counts) and `routes6`.

**Rollback.** Take the site out of `placement.ipFamiliesBySite` (no new IPv6 placements). The /125s on the peers stay
and are inert. With `edge.ipv6.enabled: false` the gateway's next run removes the IPv6 side from the routers'
configuration; addresses already on the routers stay until a `replace`.

## Cloud sites: Google Cloud

A Google Cloud site carries IPv6 in two halves as well: the platform's edge routers in the hub project (this
section) and each placement's VPC with its HA VPN and Cloud Router in the tenant project. The underlay stays IPv4:
the routers' WireGuard tunnels to the border gateways, the HA VPN tunnels' outer addresses and the BGP sessions to
the Cloud Routers. A placement's IPv6 on Google Cloud is always the provider's (source `Provider`): a Google-assigned
external /64 per subnet.

### Google Cloud edge

The edge settings live in the installation's `GCP_REALIZATION` (`edge`), next to the IPv4 ones; the generic defaults
(`common/tasks/gcp_realization.yml`) leave IPv6 off:

```yaml
edge:
  ipv6:
    enabled: false   # IPv6 on the edge routers
```
- **No next-hop pool.** Google assigns each Cloud Router peer its IPv6 next-hop pair when IPv6 is turned on for the
  peer (from Google's own range, unique per VPC). The placement job reads the pair back and hands it to the routers
  with the tunnels: the router's side, a /126, goes on the tunnel's XFRM device and is the next hop of everything the
  router announces to the Cloud Router; Google's side is the next hop of the routes it learns.
- **The site switches stay separate.** `edge.ipv6.enabled` prepares the routers; placements are offered IPv6 only
  once the site's `placement.ipFamiliesBySite` holds 6 ([Site capabilities](#site-capabilities)). Turn the edge on
  first.
- **Routers created before IPv6 need a `replace`** (`shc_gcp_edge.py replace`: new instances with the same names,
  addresses and configuration; the gateway's next run hands them the gateway nodes' keys): the agent that renders
  the IPv6 side ships with the instance. New routers also get IPv6 forwarding in their first-boot settings.

**IPsec.** Per HA VPN tunnel that carries IPv6 the router runs two IKEv2 child SAs, one per family (`0.0.0.0/0` and
`::/0`), with identical IKE parameters on the same XFRM interface: one IKE SA, both families on one device. A tunnel
without IPv6 (an IPv4-only placement, or a peer without IPv6) keeps its single child SA, configured exactly as
before. The IPsec daemon re-creates the XFRM device when it restarts, so the agent puts the device back in the VRF
with its MTU and both addresses after the daemon on every run (every 30 seconds), and once more right after it
changed the IPsec configuration.

**What the routers announce, per network VRF.** The placement job hands the routers each IPv6 placement's settings
(`shc_gcp_edge.py net-up --ipv6`): the placement's prefixes (its subnets' external /64s) and the network's IPv6
space. FRR runs IPv6 unicast on the IPv4 sessions to the Cloud Router with a route-map per family (FRR applies RFC
8212 to eBGP even with the traditional defaults):

| Direction | IPv6 |
|---|---|
| From the Cloud Router | the placement's prefixes only; never a default route |
| To the Cloud Router | the network's IPv6 space learned from the fabric, with the router's /126 address as next hop; never `::/0` (the VPC's own default route to Google's internet gateway is the internet path, present only while the network's internet egress is on) and never the placement's own prefixes |
| To the site's gateway (EVPN Type-5) | the placement's prefixes only: the provider's prefix enters the fabric for traffic between sites |

The IPv4 side is unchanged. Google advertises an external IPv6 subnet range only as a custom advertisement on the
Cloud Router peer, which the placement side keeps in step with the subnets. The network's IPv6 space never holds
another cloud's provider-assigned prefix, so a router never announces one into its own cloud: two provider-source
placements of one network on different clouds talk over the internet, both ways.

**ICMPv6 source.** Each router gets a /128 on `lo<vni>` in every IPv6 VRF, from the fabric's `core.icmpSource6`,
with the same formula as on AWS: node ordinal 100 + the router's position by name, and the gateway's provider-edge
index. The gateway job computes them and hands them to the routers with the gateway nodes' keys. The routers install
their fabric-side routes with that address as source, so Packet Too Big and Time Exceeded toward the fabric come from
it; toward the VPC they come from the XFRM device's /126 address. The routers send Packet Too Big without a rate
limit. Without the IPv6 core or `core.icmpSource6` the routers get no /128. Each VRF table also gets an unreachable
IPv6 default, so a destination nothing routes fails at once. The VRFs keep the site leg's MTU (1370 behind the
WireGuard tunnels) and the XFRM devices the VPN MTU, for both families.

**State.** `shc_gcp_edge.py status` lists per VRF its IPv6 settings and, under `ipv6`, per router the tunnels' /126
addresses, the ICMPv6 source, the sessions that negotiated IPv6 (`sessions6`, for example `1/1`), the IPv6 routes by
protocol, whether a default route is present (it should not be) and the placement prefixes not learned from the
Cloud Router yet (`missing6`). The routers' own reports carry `bgp6` (per session: `af6` negotiated or `NoNeg`, the
prefix counts), `routes6`, and both child SAs of each IPv6 tunnel under `ipsec`.

**Not done.** No IPv6 BGP sessions (IPv6 rides the IPv4 sessions), no IPv6 outer addresses on the tunnels, no
default route toward Google, and no tenant or platform prefixes on Google's side (source `Network` is not offered on
Google Cloud).

**Rollback.** Take the site out of `placement.ipFamiliesBySite` (no new IPv6 placements). With
`edge.ipv6.enabled: false` the gateway's next run removes the IPv6 side from the routers' configuration (the VRFs'
IPv6 settings, the tunnels' IPv6 next hops, the ICMPv6 sources) and the tunnels return to one child SA each;
addresses already on the routers stay until a `replace`.

## Rollout and rollback

- Order: the primary chart with both CRD copies, then the EDA content (repository seed, then the AAP configuration,
  then restart the activations), then the UI images. The CRDs go first: an object that uses a new field can block
  its own CRD in Argo CD.
- Every CRD change is a relaxation or an optional field. To roll back, delete the objects that use the new fields,
  then pin the old chart. Old automation ignores the ledger's `ipamPools` section.

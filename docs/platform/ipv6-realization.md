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
IPv6) or `AddressChoice` (tenant prefixes).

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
  writes it), a change to the fabric's spec, and a fabric teardown wake the Entities, so the profiles follow.
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
    addressMode: {CloudOSO: dhcpv6-stateful}                 # how OpenStack subnets hand out addresses
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
- **`addressMode`:** `dhcpv6-stateful` is the only OpenStack mode in which resolvers reach a guest by protocol; the
  config drive carries them in every mode.

## Site capabilities

The site job writes these booleans into `CloudInfrastructure.status.capabilities`, each the site's probe ANDed with
the switches above; they are `false` while the switches are off:

| Key | Meaning |
|---|---|
| `ipv6` | IPv6 placement subnets can be carried |
| `ipv6Only` | IPv6-only placements are offered |
| `ipv6NetworkSource` | subnets can come from the network's IPv6 space |
| `ipv6ProviderSource` | the cloud provider can assign subnets |
| `ipv6Translation` | IPv6-only placements reach IPv4 by translation |

They reach tenants through the tenant profile's site list (`IPv6`, `IPv6Only`, `IPv6NetworkSource`,
`IPv6ProviderSource`, `IPv6Translation`), so the forms show the IPv6 inputs only where a site offers them. The job's
`FamilyUnavailable` stays the authority.

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
  ipv6: {enabled: false}   # routed IPv6 egress for platform-assigned space; off: IPv6 internet reads NotOffered
```
IPv6 egress is routed, not translated: a network's platform block leaves unchanged through a stateful filter on the
border gateway nodes that admits only replies to connections opened from inside. Tenant-provided prefixes never get
an internet path. While `enabled` is false every network in mode `Platform` reports
`{family: 6, state: unavailable, reason: NotOffered}` in `status.egress.families`, and `status.egress.internet`
keeps reporting the IPv4 state of networks with IPv4.

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

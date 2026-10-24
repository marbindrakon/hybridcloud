# Hybrid networks: one private network across cloud sites

**Before you start:** sign in and find your tenant as [Getting started](../../../docs/tenant/getting-started.md)
describes; the words are explained in the [glossary](../../../docs/tenant/glossary.md). The examples use the
tenant `example-corp` (namespace `entity-example-corp`) and the sites `site-vm` (OpenShift Virtualization) and
`site-osp` (OpenStack); use your own tenant and the sites in your tenant profile.

A **network** (`HybridNetwork`) is a tenant's routed private network that spans cloud sites, with its own address
space. Each **network placement** (`NetworkPlacement`) puts the network on one cloud environment: a VM environment
(`CloudVirt`) on an OpenShift Virtualization site, an OpenStack environment (`CloudOSO`) or an AWS environment
(`CloudAWS`). The platform
gives each placement a subnet from the network's address space, of the size the tenant asks for (or a pinned
prefix). Every placement's subnets reach every other placement's subnets of the same network, and nothing else.
Two tenants can use the same prefixes, even the same addresses, without seeing each other.

The platform team runs the fabric underneath (`HybridFabric`, one `CloudGateway` per site). Tenants never
touch it.

## Who does what

| Kind | Namespace | Owner | Purpose |
|---|---|---|---|
| `HybridFabric` | platform (`sovereign-cloud`) | platform | ASN, overlay MTU, VNI pools, border gateway nodes and credentials |
| `CloudGateway` | platform | platform | attaches one site (a `CloudInfrastructure` with `evpn: true`) to the fabric |
| `CloudVirt`, `CloudOSO` | tenant (`entity-<tenant>`) | tenant | VM environment on an OpenShift Virtualization site (its namespaces); OpenStack environment |
| `HybridNetwork` | tenant | tenant | the network: address space, MTU (default and maximum: the fabric's `overlayMtu`) |
| `NetworkPlacement` | tenant | tenant | network × environment × subnet size (or pinned prefix) |

## Tenant walk-through

1. **The network:**
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: HybridNetwork
   metadata: {name: core, namespace: entity-example-corp}
   spec:
     description: shared services network
     prefixes: [10.110.0.0/16, "fd12:3456:789a::/48"]
   ```
   - `prefixes` is the network's address space: up to 8 prefixes in canonical form (host bits zero), not
     overlapping each other. IPv4 /8–/29, IPv6 /32–/64 (global unicast or unique local). The ULA above makes this
     a network with its own IPv6 prefix; a network can instead take its IPv6 from the platform, or have none
     ([IPv6 for a hybrid network](#ipv6-for-a-hybrid-network)). A network needs address space from `prefixes`,
     `platformPrefixes` or both.
   - The platform never allocates its own ranges inside the space (they are not free in `addressSpace`). A prefix
     lying wholly inside one of them is refused (`PrefixReserved`).
   - Prefixes can be added at any time. A change that would leave a placement's subnet outside the space is refused
     and names the placements (`PrefixInUse`; the space stays as it was). Removing an unused prefix, or growing one
     in place (a /16 replaced by the /15 that holds it), works.
   - It becomes Ready once the fabric is ready (`status.fabric`, `status.mtu`). `status.addressSpace` shows each
     prefix with its placement subnets (`allocations`) and the largest free block (`largestFree`, a prefix
     length).

2. **A VM environment** whose namespace `vms` uses the network, and an **OpenStack environment**:
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: CloudVirt
   metadata: {name: virt1, namespace: entity-example-corp}
   spec:
     cloudRef: {name: site-vm}
     namespaces: [{name: vms, network: core}]
     quota: {cpu: 16, memory: 64Gi, virtualMachines: 8}
   ---
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: CloudOSO
   metadata: {name: osp1, namespace: entity-example-corp}
   spec:
     cloudRef: {name: site-osp}
   ```
   - On the site the namespace is `<tenant>-<cloudvirt>-<name>`, here `example-corp-virt1-vms`.
   - A namespace's network is fixed when it is created. To move VMs, add a namespace and remove the old one.
   - VMs in a namespace with a network start only once a placement puts that network on the environment
     (`status.namespaces[].ready`).
   - Each environment gets a DNS zone `<environment>.<tenant>.<tenant DNS domain>` (`status.dns`), deleted with
     it.
   - The OpenStack environment is a project on the site; the site supplies every setting, so `cloudRef` is all it
     needs.

3. **Place the network** on both environments (in the dashboard: **Attach network** on the environment or network
   page):
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: NetworkPlacement
   metadata: {name: core-virt1, namespace: entity-example-corp}
   spec:
     network: core
     backend: {kind: CloudVirt, name: virt1}
     ipv4: {prefixLength: 24}
   ---
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: NetworkPlacement
   metadata: {name: core-osp1, namespace: entity-example-corp}
   spec:
     network: core
     backend: {kind: CloudOSO, name: osp1}
     ipv4: {prefix: 10.110.200.0/24}
   ```
   - `ipv4` is a size (`prefixLength`, /16–/29; on a CloudAWS /16–/24), or a pinned `prefix` inside the
     network's space, or both when they agree. The subnet is in `status.prefixes`. Its first usable address is the
     gateway.
   - A size gets the lowest free block of the network's IPv4 prefixes, stepping around the network's other
     placements, the platform's ranges and the site's own ranges (on an OpenShift site its cluster and service
     networks, and OVN-Kubernetes' internal subnets). With no room the placement waits (`WaitingForAddressSpace`)
     until a prefix is added or a placement goes away.
   - A pinned prefix is never replaced: outside the space, or on one of the platform's or the site's ranges, it is
     refused (`PrefixInvalid`); held by another placement, the placement waits until it is free.
   - `ipv4` and `ipv6` are each optional; a placement needs at least one. `ipv6` is a /64 on VM and OpenStack
     environments (sizes per environment type and the cloud `source` in
     [A placement's IPv6 subnet](#a-placements-ipv6-subnet)). Where the site doesn't offer IPv6 the placement fails
     with `FamilyUnavailable`, before anything is allocated.
   - `network`, `backend` (the environment), `ipv4` and `ipv6` are immutable. To resize, create a new placement.
   - On OpenStack the placement's network appears in the environment's project as a **shared network**. Attach VMs to
     it; its router and subnets belong to the platform.

4. **Use it.**
   - CloudVirt VMs on the network get their address from the placement's prefix and keep it across restarts and
     live migration. Use interface binding `l2bridge` on the pod network.
   - OpenStack VMs attach to the shared network.
   - Every placement reaches every other one of the same network at the network's MTU.

## Internet egress

A network has no route to the internet unless it asks for one. Two ways:

- **The platform's egress**, like attaching a NAT gateway in a public cloud:
  ```yaml
  spec:
    egress: {internet: true}
  ```
  - The platform advertises a default route into the network at every site it is placed on.
  - The network's traffic leaves with platform source addresses (`status.egress.sourceAddresses`; there may be
    more than one, for the platform's redundant paths).
  - Connections can only be opened from inside the network. The platform's private address ranges are not
    reachable this way.
  - `status.egress.internet`:
    - `pending` (a value of this field) while the platform adds it, `ready` once it is in place;
    - `removing` after you turn it off;
    - `unavailable` if the platform offers no internet egress.
  - It can be turned on and off at any time. A connection in progress may break when the platform moves it to
    another path, e.g. after a failure.
- **Your own edge**: leave `egress.internet` off and bring the default route through your own edge router (for
  example one that backhauls to your premises). Then the platform never adds a default route of its own.

## IPv6 for a hybrid network

A network carries IPv6 in one of three ways. You choose by the fields you fill in; there is no separate switch.
The network reports what it derived in `status.ipv6.mode` and on its page.

| Your choice (network form) | Fields | `status.ipv6.mode` | Between your sites | IPv6 internet |
|---|---|---|---|---|
| **My own prefixes** | IPv6 entries in `prefixes`: global unicast (GUA) or unique local (ULA) prefixes you own | `Tenant` | carried, like your IPv4 | none, in either direction |
| **From the platform** | `platformPrefixes` with `family: 6`; the platform assigns a block of its own globally routed space | `Platform` | carried | outbound through the platform, when the platform offers it |
| **None** | neither | `None` | no IPv6 | no IPv6 |

A network without any IPv6 field is `None`, so every IPv4-only network keeps its meaning.

### Choosing a network's IPv6

**My own prefixes** (mode `Tenant`):
```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: HybridNetwork
metadata: {name: example-net-own-ipv6, namespace: entity-example-corp}
spec:
  prefixes: [10.30.0.0/16, "2001:db8:1000::/48"]   # or a ULA such as fd12:3456:789a::/48
```
- Any prefix you own (the example's `2001:db8::/32` is the documentation range): a GUA inside `2000::/3` or a ULA inside `fc00::/7`, in canonical form, /32–/64. Link-local,
  multicast, IPv4-mapped and other special ranges are refused when you apply the object.
- The platform carries the prefix between your placements and nowhere else. It does not reach the internet and the
  internet does not reach it, whatever `egress.internet` says (the network shows `AddressChoice` for IPv6
  internet).
- The platform can't check that you own a GUA. A prefix that covers real internet addresses hides them inside your
  network only; nothing outside it changes.
- Your networks' own IPv6 prefixes may not overlap each other: a prefix that overlaps an IPv6 prefix of another
  of your networks is refused (`PrefixInUse`, which names the other network). Other tenants may use the same prefixes;
  networks never see each other.
- A prefix that overlaps address space the platform keeps for itself (its own blocks, its translation prefixes) is
  refused (`PrefixReserved`).

**From the platform** (mode `Platform`):
```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: HybridNetwork
metadata: {name: example-net-platform-ipv6, namespace: entity-example-corp}
spec:
  prefixes: [10.31.0.0/16]          # optional: leave it out for an IPv6-only network
  platformPrefixes:
    - family: 6                     # the fabric's default block size
      # prefixLength: 60            # or a size the fabric offers
      # prefix: <a free block>      # or a specific block of the platform's space
  egress: {internet: true}
```
- The platform assigns the network one block of its globally routed IPv6 space and gives every placement a /64 (or,
  on AWS, a /60 or /56) from it. `status.addressSpace` shows the block with `source: Platform`, and `status.ipv6`
  shows the block, its length and whether it was pinned or allocated.
- Which block sizes a fabric offers, and the default when you name none, is the platform's choice: a size the
  fabric doesn't offer is refused (`PrefixInvalid`, the message lists the sizes). A fabric that has no platform IPv6
  space refuses the request (`FamilyUnavailable`).
- Each tenant may hold a limited number of platform blocks. The network waits (`WaitingForAddressSpace`) while the
  platform has no free block, while your tenant's allowance is used, or while the block you pinned belongs to
  another network; the message says which, and the network continues by itself once a block is free.
- When a network is deleted (or stops asking for a block), its block is held back for some days before another
  network gets it, so a new owner never inherits old DNS records or allow-lists. While a network waits for a block
  that is held back, `status.ipv6.holdDownUntil` shows until when.
- A `platformPrefixes` entry can't be edited: remove it and add another. Removing it is refused while a placement
  uses the block (`PrefixInUse`).

**Rules for every network:**
- A network needs address space: `prefixes`, `platformPrefixes` or both. It may be IPv6-only (no IPv4 prefix),
  IPv4-only, or dual-stack.
- One IPv6 source per network: IPv6 entries in `prefixes` and `platformPrefixes` with `family: 6` together are
  refused when you apply the object.
- **The mode can't change while the network has placements** (`IPv6ModeInUse`; the network keeps working with the
  mode it had). Without placements, change the fields as you like. Inside mode `Tenant`, adding and removing your own
  IPv6 prefixes follows the usual rules for `prefixes`.

### Internet access per family

`egress.internet: true` asks for internet access for every family the network has. What each family gets is in
`status.egress.families`:

| Network | Row | What it means |
|---|---|---|
| has IPv4 | `{family: 4, state: ready, mode: SourceNAT}` | IPv4 leaves through source address translation ([Internet egress](#internet-egress)). |
| mode `Platform` | `{family: 6, state: ready, mode: Routed}` | IPv6 leaves unchanged from your platform addresses through a filter that admits only replies to connections opened from inside. |
| mode `Platform`, where the platform doesn't offer IPv6 internet | `{family: 6, state: unavailable, reason: NotOffered}` | The network is Ready; IPv6 stays inside the network until the platform offers IPv6 internet. |
| mode `Tenant` | `{family: 6, state: unavailable, reason: AddressChoice}` | Your own prefixes have no internet access. Use a network with IPv6 from the platform for that. |
| mode `None` | no IPv6 row | |

- `status.egress.internet` keeps its meaning: the IPv4 state on a network with IPv4, the IPv6 state on an IPv6-only
  network.
- Nothing unsolicited reaches your IPv6 addresses from the internet; inbound IPv6 is not offered.
- When the platform moves IPv6 traffic to another path after a failure, the row may read `ready` with reason
  `Degraded`; connections in progress may reset.
- **Translation to IPv4.** A network with IPv6 from the platform, no IPv4 and internet access on reaches IPv4-only
  destinations by translation where the platform offers it (`status.egress.ipv4Translation`; on each placement
  `status.ipv6.translation` gives the translation prefix and the resolvers to use). Dual-stack networks never get
  translation: they use their own IPv4. There is no switch for it.
- Your own edge: edge connections carry IPv4 only; they don't accept IPv6 routes from your router yet.

### A placement's IPv6 subnet

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: NetworkPlacement
metadata: {name: example-net-platform-ipv6-vms, namespace: entity-example-corp}
spec:
  network: example-net-platform-ipv6
  backend: {kind: CloudVirt, name: example-vms}
  ipv4: {prefixLength: 24}
  ipv6: {prefixLength: 64}
```
- `ipv4` and `ipv6` are both optional; a placement needs at least one. Neither can be added or removed later.
- `ipv6` is a size (`prefixLength`) or a pinned `prefix` inside the network's IPv6 space, or both when they agree:

  | Environment | IPv6 subnet | Source |
  |---|---|---|
  | VM environment, OpenStack environment | /64 | the network's space |
  | AWS environment | /60 or /56 (an AWS VPC takes IPv6 blocks of /44 to /60); without a size the site's default | `Network` (the network's space) or `Provider` (Amazon assigns a /56) |
  | Google Cloud environment | assigned by Google (a /64 per subnet) | `Provider` only |

- **`source`** (cloud environments only): `Network` takes the subnet from the network's IPv6 space, and its
  internet traffic leaves through the platform (extra latency and data transfer, the platform's filter). `Provider`
  takes a range from the cloud provider, with the provider's own internet path; give no `prefixLength` or `prefix`
  then. `Provider` needs a network with IPv6 from the platform. When you leave `source` out, the site's default
  applies; the placement reports the result. A Google Cloud site can't carry your own IPv6 prefixes.
- The placement reports one row per family in `status.subnets` (`family`, `prefix`, `source`, and the IPv4
  `gateway`); `status.prefixes` keeps listing the prefixes, IPv4 first. `status.ipv6` shows how guests get their
  address (`addressing`: `DHCPv6` or `SLAAC`), the internet path (`internet.path`: `Fabric` or `Provider`) and, for
  IPv6-only placements with translation, `translation`.
- **Where IPv6 is offered:** a site offers IPv6 placements only once the platform team has turned IPv6 on for it,
  and some sites offer IPv6-only placements or only some sources. Elsewhere the placement fails with
  `FamilyUnavailable` before anything is allocated, and the message names what is missing:
  - "IPv6 is not available on this environment's site";
  - "IPv6-only placements are not offered on this site";
  - "the network has no IPv6 address space" (an `ipv6` placement of a network in mode `None`);
  - "this site can't carry tenant-provided IPv6 prefixes", or "this site offers IPv6 only from the cloud provider;
    use source Provider".

  `source: Provider` on a network with your own prefixes is refused with `PrefixInvalid` ("provider IPv6 needs a
  network with IPv6 from the platform").
- OpenShift clusters need IPv4: they can't be built on an IPv6-only network.
- The router's addresses in the subnet belong to the platform. Guests learn the IPv6 default route from router
  advertisements or the config drive; don't hard-code it.

### Guests on IPv6

**OpenShift Virtualization environments**
- VMs get their IPv6 address by stateful DHCPv6 (the router advertisement sets the M flag and gives the default
  route); the address stays across restarts and live migration.
- **The guest's IPv6 link-local address must be the EUI-64 one** (derived from the MAC address). The network drops
  DHCPv6 and neighbour discovery from any other link-local address, so a guest that uses stable-privacy or random
  interface identifiers gets no IPv6 address. Some images do that by default (RHEL with NetworkManager, Windows).
- Always give cloud-init `networkData` with `dhcp6: true` (without it, cloud-init may leave IPv6 off). Match the
  interface by its MAC address or its exact name: a name pattern such as `en*` is ignored by some renderers. On
  images whose NetworkManager defaults to stable privacy, switch the profile to EUI-64 at first boot:
  ```yaml
  #cloud-config
  runcmd:
    - |
      for c in $(nmcli -g UUID,TYPE connection show --active | awk -F: '$2 == "802-3-ethernet" {print $1}'); do
        nmcli connection modify "$c" ipv6.addr-gen-mode eui64 ipv6.method auto && nmcli connection up "$c"
      done
  ```
- DHCPv6 gives no DNS search domain; set it in cloud-init.

**OpenStack environments**
- Subnets are DHCPv6-stateful: the address, the default route and the resolvers reach the guest by DHCPv6 and in
  the config drive.
- **Boot VMs with a config drive** (`openstack server create --config-drive true ...`) when they use IPv6, and
  always when the placement is IPv6-only. The config drive carries the IPv6 address, route and resolvers, so the
  guest needs neither the link-local metadata service (stock cloud-init can't reach it over IPv6) nor DHCPv4.
  cloud-init then sets a static IPv6 address and ignores router advertisements.
- The site sends no DNS servers in router advertisements. A guest that configures IPv6 from router advertisements
  alone (SLAAC) gets no IPv6 resolver; some images (Debian with netplan) then have no resolver at all on an IPv6-only
  placement. Use the config drive, or set the resolvers yourself.
- **Router advertisements from other VMs:** on OpenStack a VM can send router advertisements that reach the other VMs
  of the placement. Guests configured from the config drive ignore them; a guest that accepts router advertisements
  can be misled by a neighbour of the same network. Keep guests on the config drive's settings, or filter router
  advertisements in the guest.

**IPv6-only guests and translation**
- Translation needs a CLAT in the guest: **clatd** (2.1 or later) with its default `clat-v6-addr=shared`, which needs
  no second address on the port (the network drops traffic from addresses it did not assign). Some distributions
  package it (Fedora, Debian); on others build it yourself. Windows guests are not covered.
- clatd finds the translation prefix by the standard DNS query (RFC 7050) through the resolvers in
  `status.ipv6.translation`; the sites put no translation prefix in router advertisements.
- **Keep clatd's IPv4 route MTU of 1260.** The translator fragments translated replies larger than 1280 bytes, and
  those fragments are lost on the way into the CLAT: with a larger MTU, IPv4 datagrams that must not be fragmented
  are lost above 1232 bytes of payload. TCP is not affected.
- On OpenShift Virtualization the resolvers come from the placement's status: the placement page offers a cloud-init
  `networkData` and clatd snippet built from it, to copy into the VM.
- A guest that validates DNSSEC itself gets no translated addresses for signed IPv4-only names. Reach those by an
  IPv4 address through the CLAT, or let the resolver validate.

**Every guest**
- **DNS on dual-stack guests:** they keep the IPv4 resolvers they get by DHCP first and never use translation, so
  IPv4-only names are reached over native IPv4; for names with both kinds of address they prefer IPv6. The platform
  hands out IPv6 resolvers only to IPv6-only placements.
- Keep ICMPv6 open in guest firewalls and security groups: neighbour discovery and Packet Too Big are required. The
  path to the IPv6 internet can be smaller than the network's MTU, and the platform reports that with Packet Too Big.

### Adding IPv6 later

- **A network without placements:** add `platformPrefixes` or your own IPv6 prefixes and apply.
- **A network with placements:** its mode can't change (`IPv6ModeInUse`). Create a new network with the IPv6 you
  want, place it, and move the workloads: on a VM environment add a namespace for the new network, move the VMs and
  remove the old namespace; on an OpenStack environment attach the VMs to the new shared network, then detach them
  from the old one. Then delete the old placements and network.
- **A placement without `ipv6`:** `ipv6` can't be added in place. Once the network has IPv6, create the placement
  again with `ipv6` (on a VM environment, the placement's delete waits for the namespaces naming the network), or
  move to a new network as above.

## DNS names in the environment zone

A VM environment's zone `<cloudvirt>.<tenant>.<tenant DNS domain>` (`status.dns.zone`) is held by the platform.
Tenants publish names in it from their VM namespaces, without a second identity:

- a **`DNSEndpoint`** (`externaldns.k8s.io/v1alpha1`, the external-dns schema):
  ```yaml
  apiVersion: externaldns.k8s.io/v1alpha1
  kind: DNSEndpoint
  metadata: {name: web, namespace: example-corp-virt1-vms}
  spec:
    endpoints:
      - {dnsName: "www.virt1.example-corp.<tenant DNS domain>", recordType: A, targets: [10.110.1.10], recordTTL: 300}
  ```
- or the annotation `external-dns.alpha.kubernetes.io/hostname` (comma-separated names) on a **Service** or a
  **Route**. The targets come from `external-dns.alpha.kubernetes.io/target`, else the Service's load balancer
  address or the Route's router (CNAME). `external-dns.alpha.kubernetes.io/ttl` sets the TTL.

Rules:
- Names must be in the namespace's environment zone (or be its apex, not for CNAME). Types: A, AAAA, CNAME, TXT,
  MX, SRV and CAA.
- A name and type belongs to one object: the one that holds it keeps it, otherwise the oldest object gets it.
- The result is in the object's annotation `hybridsovereign.redhat/dns-status` (`ok: …` or `refused: …`, plus
  what was published) and in Events (`DNSRecordsPublished`, `DNSRecordRefused`).
- A record goes away with its object, or when its name leaves the object. Deleting the environment deletes the
  zone with everything in it.
- Names are public; private addresses are allowed in them.

## In the console and the dashboards

The OpenShift console's **Sovereign Cloud** pages and the standalone dashboards (tenant and admin) show the same
pages. People sign in as [Getting started](../../../docs/tenant/getting-started.md) describes; the roles their access
groups hold on the tenant decide what they may do in the tenant namespace.

Tenants:
- **Networks → Network map:** the tenant's networks against its environments. A cell is one network placement
  (state and prefixes); each network shows its MTU and internet egress (state and source addresses).
- **Networks → Networks:** create and edit (address space, description, MTU, internet egress, viewers). The form's
  IPv6 choice has three options: **From the platform** (a block size from the sizes the fabric offers, or a
  specific block), **My own prefixes** (your GUA or ULA prefixes) and **None**. **From the platform** is preselected
  when the form can tell that the fabric offers platform IPv6, otherwise **None**; no prefix is filled in for you. Once the network has
  placements the choice is locked, with the reason. The detail page shows the IPv6 mode, the address space in use
  with a source column (yours or the platform's), the effective MTU, placements ready, internet egress per family
  with its reason (for example "IPv6 internet: not offered for tenant-provided prefixes") and, collapsed, the
  realization (platform detail).
- **Networks → Network placements**, or **Attach network** on an environment or network page: create (network,
  environment, subnet size or pinned prefix, per family); read-only afterwards, since those fields are immutable.
  The dialog has an IPv4 and an IPv6 section, each optional, at least one. The IPv6 section appears only when the
  network has IPv6 and the environment's site offers IPv6; on cloud sites it also offers the IPv6 source (from the
  network, or from the cloud provider) with the site's default preselected. Options a site lacks are greyed out
  with the reason.
- **A placement's page** shows each family's subnet and source, how guests get their IPv6 address, the internet
  path and, on IPv6-only placements with translation, the translation prefix and resolvers with a cloud-init
  snippet to copy.
- **Environments → OpenShift Virtualization:** environments with their site, VM namespaces (network, ready), quota,
  DNS zone and API endpoint. The site picker offers the sites of the tenant profile.
- Platform objects aren't shown to tenants.

Platform team (**Fabric and sites**):
- **Fabric map:** each fabric (ASN, overlay MTU, border gateway nodes ready, pool use), a map of the sites attached
  through site gateways (site type, transport, BGP sessions per node, tunnel state for remote sites), and every
  tenant's networks.
- **Sites, Fabric, Site gateways:** list, detail, create and edit. Forms show only the section for the chosen site
  type or transport. A gateway's detail page lists each node's sessions and, for WireGuard, each node's public key and
  each peer's endpoint and last handshake.

## Status

Each object shows a state (Waiting, Creating, Updating, Ready, Degraded, Failed, Deleting), a one-line summary and
the reason of its `Ready` condition; [Status reasons](../../../docs/tenant/status-reasons.md) lists every reason and
who acts on it. The reasons you meet most with networks:

| State | Common reasons |
|---|---|
| Waiting | `WaitingForDependencies` (network, environment or site connection not usable yet), `WaitingForAddressSpace` (no room in the network's space, a pinned prefix held by another placement, or no platform IPv6 block free for the network yet), `WaitingForGateway` (the site is adding the network) |
| Ready | realized; `status.realization` shows how (platform detail) |
| Failed | the spec can't be realized as written: `PrefixReserved`, `PrefixInUse`, `PrefixInvalid`, `FamilyUnavailable`, `IPv6ModeInUse`, `FabricMismatch`, `EntityNotAllowed`, `SiteNotEvpnCapable` (the site cannot join networks), `NamespaceTaken`, `EnvironmentNameTaken`, ...; or the platform team has to act: `RoutesMissing` (the site hasn't advertised the placement's prefixes) |

A dependency counts as usable when it is ready, or realized before (`status.realized`) and not failed. A
network or gateway that is re-running doesn't stop its placements.

## Deleting

Deletes wait for what depends on the object (`status.deletionBlockedBy`):

- a VM environment waits for the **VirtualMachines** in its namespaces (delete them first);
- a network placement on a VM environment waits while the environment still has a namespace naming its network;
  on an OpenStack environment it waits for the tenant's **ports** on the network (delete those VMs first);
- a network waits for its placements and for VM environment namespaces naming it; its address space goes with it;
- site gateways and fabrics (platform) wait for the placements and networks that use them.

Deleting everything at once works: the guards order it.

## Platform operations

For the platform team; tenants can skip the rest of this page. Onboarding a tenant (sites, roles, access groups) is in
[Onboard a tenant](../../../docs/platform/onboard-a-tenant.md).

- **Realization parameters** (how this platform builds the fabric) live in the EDA vars
  `common/vars/fabric_realization.yml` and the installation's `FABRIC_REALIZATION`, not in the CRs. Among them:
  `ipam.provider` (where address spaces and placement subnets are kept; `ledger`, the fabric's ledger, is the only
  one so far), `placement.ipFamilies` (families offered per backend kind) and `placement.siteReserved` (site ranges
  the allocator steps around, per backend kind). The IPv6 keys (platform pools, block sizes, the tenant allowance,
  hold-down, exclusive ranges, per-site switches and sources) are in
  [IPv6 realization](../../../docs/platform/ipv6-realization.md).
- **`fabric-orphan-check`** (AAP job template, report only) compares what the fabric jobs realized with the CRs:
  - ledger entries without owners, including address spaces and placement subnets (the IPAM), and placements
    whose subnets the IPAM doesn't hold for them;
  - networks, advertisements and CloudVirt namespaces on the sites with the platform's labels;
  - networks and routers in the OpenStack platform project;
  - gateway namespaces and VRFs on the border gateway nodes.

  The job fails when something has no owner.
- **Re-running** any fabric object (Retry now: write a new timestamp into
  `ansible.sdk.operatorframework.io/reconcileNow`) is safe: an unchanged gateway reports "no change" and nothing
  flaps.

## This platform's realization (informational)

- **Border gateway nodes:** Linux hosts with FRR. Each CloudGateway is a network namespace per node: the site's
  EVPN route reflector and a VTEP per placed network on the site side, and an MPLS L3VPN provider edge (IS-IS
  segment routing, VPNv4 to the fabric's route reflector) on the fabric side. Every node is a complete path, and
  the sites use BFD to leave a failed node within a second.
- **OpenShift sites:** a Layer2 primary ClusterUserDefinedNetwork with EVPN transport per placement, plus
  RouteAdvertisements.
- **OpenStack sites:** a network, subnets and a router with the placement's EVPN VNI in a platform project,
  shared to the tenant project.
- **Remote sites** (`CloudGateway.spec.transport: wireguard`): the gateway's site leg on every node is a WireGuard
  tunnel to the site's peers (`spec.wireguard`), and `siteUnderlay.cidr` is the tunnel's subnet. Each node
  generates its key pair once and keeps the private key; the public keys and each peer's last handshake are in
  `status.wireguard`, and a node's tunnel shows `up`/`down` in `status.nodes`. A site whose network the platform
  doesn't run (a remote data center, a partner's EVPN fabric) is a `CloudInfrastructure` of type `external`; its
  own operator configures its VTEPs with the gateway's public keys and tunnel addresses. The tunnel's MTU (default
  1420, for a 1500 path) caps what networks placed there can use.
- **Internet egress:** each border gateway node's root holds an egress VRF that hands traffic to the site network
  (a macvlan; default route via the network's router, whose own NAT reaches the internet). Per network with
  egress: a VRF `e<vpnTarget>` that imports the network's VPN routes and exports a default route into it, a veth
  into the egress VRF, and source NAT to one address per node from that node's NAT pool (nftables table
  `shc_egress`: a conntrack zone per network, so overlapping tenant prefixes don't collide; no private
  destinations; nothing unsolicited inbound). The HybridFabric job is the only writer. Settings: realization
  `egress` (hand-off interface, gateway, per-node address and NAT pool, deny/allow lists).
- **Tenant DNS on OpenShift sites:** a controller per site (`shc-tenant-dns`, from the cluster base layer) watches
  the CloudVirt namespaces and writes the records into the environment zone in the platform's DNS project
  (Designate). It trusts only what the platform sets: the namespace label `hybridsovereign.redhat/cloudvirt` and
  its zone annotation `hybridsovereign.redhat/dns-zone`. It marks its recordsets as its own (description
  `shc-tenant-dns cluster=<site> owner=<namespace>/<kind>/<name>`) and never changes others.

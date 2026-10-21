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
   - `prefixes` is the network's address space: 1–8 prefixes in canonical form (host bits zero), at least one
     IPv4, not overlapping each other. IPv4 /8–/29, IPv6 /32–/64.
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
   - `ipv6` (optional, /64) is accepted, but where the platform doesn't offer IPv6 for the environment type the placement
     fails with `FamilyUnavailable`, before anything is allocated.
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
- **Networks → Networks:** create and edit (address space, description, MTU, internet egress, viewers); the detail
  page shows the address space in use, the effective MTU, placements ready, egress and, collapsed, the realization
  (platform detail).
- **Networks → Network placements**, or **Attach network** on an environment or network page: create (network,
  environment, subnet size or pinned prefix); read-only afterwards, since those fields are immutable.
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
| Waiting | `WaitingForDependencies` (network, environment or site connection not usable yet), `WaitingForAddressSpace` (no room in the network's space, or a pinned prefix held by another placement), `WaitingForGateway` (the site is adding the network) |
| Ready | realized; `status.realization` shows how (platform detail) |
| Failed | the spec can't be realized as written: `PrefixReserved`, `PrefixInUse`, `PrefixInvalid`, `FamilyUnavailable`, `FabricMismatch`, `EntityNotAllowed`, `SiteNotEvpnCapable` (the site cannot join networks), `NamespaceTaken`, `EnvironmentNameTaken`, ...; or the platform team has to act: `RoutesMissing` (the site hasn't advertised the placement's prefixes) |

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
  the allocator steps around, per backend kind).
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

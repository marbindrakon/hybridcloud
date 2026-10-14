# Hybrid networks: one private network across cloud sites

A **HybridNetwork** is a tenant's routed private network that spans cloud sites. Each **NetworkPlacement** puts
the network on one cloud environment, either a VM environment (**CloudVirt**) on an OpenShift Virtualization site
or an OpenStack project (**CloudOSO**), with one or more prefixes. Every placement's subnets reach every other
placement's subnets of the same network, and nothing else. Two tenants can use the same prefixes, even the same
addresses, without seeing each other.

The platform team runs the fabric underneath (**HybridFabric**, one **CloudGateway** per site). Tenants never
touch it.

## Who does what

| Kind | Namespace | Owner | Purpose |
|---|---|---|---|
| `HybridFabric` | platform (`sovereign-cloud`) | platform | ASN, overlay MTU, VNI pools, border gateway nodes and credentials |
| `CloudGateway` | platform | platform | attaches one site (a `CloudInfrastructure` with `evpn: true`) to the fabric |
| `CloudVirt` | entity | tenant | VM environment on an OpenShift site; its namespaces |
| `HybridNetwork` | entity | tenant | the network: name, MTU (default and maximum: the fabric's `overlayMtu`) |
| `NetworkPlacement` | entity | tenant | network × environment × prefixes |

## Tenant walk-through

1. **The network:**
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: HybridNetwork
   metadata: {name: core, namespace: entity-acme-corp}
   spec: {description: shared services network}
   ```
   It becomes `ready` once the fabric is ready (`status.fabric`, `status.mtu`).

2. **A VM environment** whose namespace `vms` uses the network:
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: CloudVirt
   metadata: {name: virt1, namespace: entity-acme-corp}
   spec:
     cloudRef: {name: iaas1}
     namespaces: [{name: vms, network: core}]
     quota: {cpu: 16, memory: 64Gi, virtualMachines: 8}
   ```
   - On the site the namespace is `<entity>-<cloudvirt>-<name>`, here `acme-corp-virt1-vms`.
   - A namespace's network is fixed when it is created. To move VMs, add a namespace and remove the old one.
   - VMs in a namespace with a network start only once a placement puts that network on the environment
     (`status.namespaces[].ready`).
   - The environment gets a DNS zone `<cloudvirt>.<entity>.<tenant domain>` (`status.dns`), deleted with it.

3. **Place the network** on the environment, and on an OpenStack project:
   ```yaml
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: NetworkPlacement
   metadata: {name: core-virt1, namespace: entity-acme-corp}
   spec:
     network: core
     backend: {kind: CloudVirt, name: virt1}
     prefixes: [10.110.1.0/24]
   ---
   apiVersion: hybridsovereign.redhat/v1alpha1
   kind: NetworkPlacement
   metadata: {name: core-oso1, namespace: entity-acme-corp}
   spec:
     network: core
     backend: {kind: CloudOSO, name: oso1}
     prefixes: [10.110.2.0/24]
   ```
   - `network`, `backend` and `prefixes` are immutable.
   - Prefixes must not overlap each other, the network's other placements, or the site's own networks.
   - On OpenStack the placement's network appears in the tenant's project as a **shared network**. Attach VMs to
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
    - `pending` while the platform adds it, `ready` once it is in place;
    - `removing` after you turn it off;
    - `unavailable` if the platform offers no internet egress.
  - It can be turned on and off at any time. A connection in progress may break when the platform moves it to
    another path, e.g. after a failure.
- **Your own edge**: leave `egress.internet` off and bring the default route through your own edge router (for
  example one that backhauls to your premises). Then the platform never adds a default route of its own.

## DNS names in the environment zone

A CloudVirt environment's zone `<cloudvirt>.<entity>.<tenant domain>` (`status.dns.zone`) is held by the platform.
Tenants publish names in it from their VM namespaces, without a second identity:

- a **`DNSEndpoint`** (`externaldns.k8s.io/v1alpha1`, the external-dns schema):
  ```yaml
  apiVersion: externaldns.k8s.io/v1alpha1
  kind: DNSEndpoint
  metadata: {name: web, namespace: acme-corp-virt1-vms}
  spec:
    endpoints:
      - {dnsName: www.virt1.acme-corp.tenants.shc.signal9.gg, recordType: A, targets: [10.110.1.10], recordTTL: 300}
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

## Status

| `status.status` | Meaning |
|---|---|
| `reconciling` | a job is running |
| `pending` | waiting for something; `status.waitingFor` says what, and the job is retried on the CR's period. Common reasons: `WaitingForDependencies` (network, backend or gateway not usable yet), `WaitingForGateway` (the site's gateway is adding the network), `RoutesMissing` (the site hasn't advertised the placement's prefixes yet), `SiteNotEvpnCapable` |
| `ready` | realized; `status.realization` shows how (informational) |
| `failed` | the spec can't be realized as written; the condition's reason says why (`PrefixOverlap`, `FabricMismatch`, `EntityNotAllowed`, `NamespaceTaken`, `EnvironmentNameTaken`, …) |

A dependency counts as usable when it is ready, or realized before (`status.realized`) and not failed. A
network or gateway that is re-running doesn't stop its placements.

## Deleting

Deletes wait for what depends on the object (`status.deletionBlockedBy`):

- a **CloudVirt** waits for the **VirtualMachines** in its namespaces (delete them first);
- a **NetworkPlacement** on a CloudVirt waits while the environment still has a namespace naming its network;
  on a CloudOSO it waits for the tenant's **ports** on the network (delete those VMs first);
- a **HybridNetwork** waits for its placements and for CloudVirt namespaces naming it;
- **CloudGateway** and **HybridFabric** (platform) wait for the placements and networks that use them.

Deleting everything at once works: the guards order it.

## Platform operations

- **Realization parameters** (how this platform builds the fabric) live in the EDA vars
  `common/vars/fabric_realization.yml` and the installation's `FABRIC_REALIZATION`, not in the CRs.
- **`fabric-orphan-check`** (AAP job template, report only) compares what the fabric jobs realized with the CRs:
  - ledger entries without owners;
  - networks, advertisements and CloudVirt namespaces on the sites with the platform's labels;
  - networks and routers in the OpenStack platform project;
  - gateway namespaces and VRFs on the border gateway nodes.

  The job fails when something has no owner.
- **Re-running** any fabric CR (`ansible.sdk.operatorframework.io/reconcileNow: "true"`) is safe: an unchanged
  gateway reports "no change" and nothing flaps.

## This platform's realization (informational)

- **Border gateway nodes:** Linux hosts with FRR. Each CloudGateway is a network namespace per node: the site's
  EVPN route reflector and a VTEP per placed network on the site side, and an MPLS L3VPN provider edge (IS-IS
  segment routing, VPNv4 to the fabric's route reflector) on the fabric side. Every node is a complete path, and
  the sites use BFD to leave a failed node within a second.
- **OpenShift sites:** a Layer2 primary ClusterUserDefinedNetwork with EVPN transport per placement, plus
  RouteAdvertisements.
- **OpenStack sites:** a network, subnets and a router with the placement's EVPN VNI in a platform project,
  shared to the tenant project.
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

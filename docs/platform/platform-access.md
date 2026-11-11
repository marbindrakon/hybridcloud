# Platform access to tenant networks

For the platform team. What a deployment needs so that the platform can build and manage OpenShift clusters on
tenant networks: the hub's address ranges, the service routing domain on the border gateway nodes, the platform's
edge connections to the hub's network, what the hub's network must do, what OpenStack sites must advertise, and
DNS. The requirements are collected in the [checklist](#checklist) at the end.

The **hub** is the cluster where the platform's management runs: the cluster installation (Hive) and cluster
management (ACM) that build a tenant's OpenShift cluster and keep it managed. A cluster on a tenant network has its
API and ingress addresses on that network, so the hub must reach the network. It does so through the **service
routing domain**, a VRF on the border gateway nodes that only the platform uses. The platform's own **edge
connections** (`AttachmentCircuit` with `ceKind: platform`) hand that domain off to the hub's network over a VLAN and
eBGP, as a customer's edge connection hands a network off to the customer's router.

Nothing is reserved or routed for a network until a cluster is built on it: the domain holds the addresses of the
clusters (the API and ingress floating IPs of an OpenStack cluster, the placement subnet of an AWS or Google Cloud
cluster that publishes Internal), first come first served, and the network gets its link to the domain with its
first cluster and loses it with its last.

## The realization settings

The settings live in the realization (`common/vars/fabric_realization.yml` ships them off and empty) and in the
installation's `FABRIC_REALIZATION` (per fabric under `fabrics.<fabric name>`). The example uses documentation
ranges; put the deployment's own there.

```yaml
infrastructure:
  # the networks the platform's management connects from (the hub's node and service networks); exclusive
  hub: [198.51.100.0/24]
service:
  enabled: true
  vrf: {name: service, table: 9001}
  # TCP ports the hub may open toward a cluster's addresses
  ports: [6443, 443]
  # addresses in the hub's ranges the clusters may open TCP connections to (the hub's API for cluster management)
  hubEndpoints:
    - {prefix: 198.51.100.10/32, ports: [6443]}
  # the ranges a cluster's addresses must lie in; the platform announces nothing outside them
  attachableRanges: [10.64.0.0/10]
```

- **`infrastructure.hub`**: every range the hub's management connects from, either family. No tenant address space,
  edge connection route or cluster network may overlap these ranges: a tenant prefix inside one is refused
  (`PrefixReserved`), and a realization pool that overlaps one fails the fabric (`InvalidSpec`, naming both keys).
  Networks created before the range was declared keep working; their `PlatformReachable` reads False
  `InfrastructureOverlap`. The traffic of Hive and ACM toward a cluster must leave the hub from addresses inside
  these ranges: when the hub's nodes translate their pods' traffic to the node addresses, the node network belongs
  here.
- **`service.enabled`** needs `attachments.enabled` and a non-empty IPv4 `infrastructure.hub`.
- **`service.vrf`**: a name other than the egress VRF's and not of the form `e<digits>`; a table outside 253–255,
  not the egress table and below `egress.networkTableBase`.
- **`service.ports`**: what the hub may open toward a cluster's addresses. ICMP echo is admitted too.
- **`service.hubEndpoints`**: what a cluster may open toward the hub. Each prefix must lie inside
  `infrastructure.hub`. Empty: the clusters reach the hub through their network's own outbound path (internet
  egress), and the hub's API address must then be reachable that way. When the hub's API address lies inside the
  hub's ranges and no entry admits it on 6443, a cluster on a network is refused before it is built
  (`HubEndpointNotAdmitted`), because cluster management could not reach the hub from it. With entries set, an
  OpenStack cluster's router gateway address joins the cluster's addresses after the install, so the nodes' own
  traffic to the endpoints is admitted.
- **`service.attachableRanges`** (required with `service.enabled`): IPv4 ranges a cluster's addresses must lie in,
  clear of the reserved ranges. The platform announces nothing outside them, so the hub's network can accept them
  with a fixed filter. `0.0.0.0/0` is the explicit "any range that passes the other checks" (the hub's network then
  accepts any prefix up to /32 from the platform); an empty list fails the fabric with `InvalidSpec`. Tenants see the list as "Ranges for clusters managed by the platform"; a
  network with no IPv4 prefix inside it reads `PlatformReachable` False `OutsideAttachableRanges`.

The settings that replace them, `egress.platformSources` and `egress.platformPorts`, are refused by the fabric while
they are still set ("replaced by infrastructure.hub and service.ports").

## The platform's edge connections

A provider edge bearer offers the platform kind when its `ceKinds` lists `platform`:

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: ProviderEdge
metadata:
  name: example-edge
  namespace: <fabric namespace>
spec:
  fabricRef: {name: example-fabric}
  platform: frr
  bearers:
    - name: hub
      ceKinds: [platform]
```

An edge connection of the platform kind names no network and accepts no routes of its own: it accepts the hub's
ranges and announces the clusters' addresses. It lives in the fabric's namespace (tenants cannot write there, and
the tenant dashboard refuses the platform kind); it is refused when the service domain is off
(`ServiceNotOffered`) and for IPv6 (`FamilyUnavailable`). Two connections (two hub routers, or two bearers) give
redundancy; all of them sit in the service domain.

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: AttachmentCircuit
metadata:
  name: hub-a
  namespace: <fabric namespace>
spec:
  ceKind: platform
  edgeRef: {name: example-edge, bearer: hub}
  routing:
    protocol: bgp
    bgp:
      peerAsn: 64511
      authentication: {type: md5, secretRef: {name: hub-a-bgp}}
      bfd: true
```

The connection's `status.ceConfig` is what the hub-side router needs: the VLAN, the platform's AS number, the link
addresses and session per border gateway node, `advertisedRanges` (the prefix list of what the platform announces:
each `service.attachableRanges` entry up to /32) and `acceptedRanges` (the IPv4
`infrastructure.hub` ranges up to /32). `status.sessions` shows each node's session.

## What the hub's network must do

The hub's network must route the tenant cluster addresses through the platform's edge connections. On the
hub-side router:

1. A VLAN interface on the bearer's link for each connection, with the router's link address from `status.ceConfig`.
2. An eBGP session with every border gateway node listed in `status.ceConfig` (password, timers and BFD as the
   connection says).
3. Import: accept `advertisedRanges` only, with a fixed filter; the platform never announces anything else.
4. Export: announce the hub's ranges (`acceptedRanges`); the platform accepts nothing else.
5. Route the learned cluster addresses toward the connections from the hub's node and service networks, so that
   the hub's traffic to a cluster (Hive's install, ACM's import and management) takes them.

What the platform admits through the domain: from the hub's ranges, TCP on `service.ports` and ICMP echo, only to
the cluster addresses it accepted; from a cluster's addresses, TCP to `service.hubEndpoints` only; ICMP errors both
ways. No tenant network reaches another through the domain, and no route crosses from it into a tenant network
beyond the hub's ranges, which a network learns only while it hosts a cluster. The filter is stateless (the two
directions of a connection may cross different nodes).

## OpenStack sites: floating IPs on placed networks

An OpenStack cluster on a network has its router's external gateway and its API and ingress floating IPs on the
placed network. The placement exposes the network to the environment's project only ("Use for router
attachments": a per-project external network), never cloud-wide. The site must advertise those NAT addresses:

- **Requirement:** the site's EVPN advertises the NAT addresses of routers whose gateway is on a placed network.
  With OVN this needs OVN 25.03 or later and `nat` in the EVPN router port's
  `options:dynamic-routing-redistribute`. Neutron's EVPN service plugin sets `connected-as-host` or
  `static,connected-as-host` there, not `nat`, so the site adds it.
- **Check one router port** on the site (the port of the placed network's router that carries the EVPN option):

  ```
  ovn-nbctl get Logical_Router_Port <port> options:dynamic-routing-redistribute
  ```

  The value must list `nat`, for example `"connected-as-host,nat"`. Check again after the placement's router was
  re-created (a placement's Retry now can re-add the port, and an option set by hand on the port goes with it).
- **The symptom when it is missing:** the cluster's preflight passes, the floating IPs are allocated and recorded,
  and the install never reaches the cluster: Hive's install fails after its timeout and the cluster ends Failed
  with "The platform could not reach the cluster at <API floating IP>. Clusters on a network need the platform's
  connection to the network and a site that advertises floating IPs." The same holds for a tenant's own routers
  with floating IPs on the network: those addresses have no path off the site.

The platform does not detect the setting; it is a documented requirement and assumed.

A site's **global external networks** (networks every project may use, with Neutron's `access_as_external` RBAC
policy for all projects, which Neutron creates when a network's `router:external` flag is set) are found on every
site run and listed in `CloudInfrastructure status.site.externalNetworks`; tenants see them in their tenant profile
and choose one for a cluster that is not on a network of theirs. A network external for some projects only is never
listed. A network whose flag was set by an older release without the policy is not found: check with
`openstack network rbac list --action access_as_external`.

## DNS

The cluster records keep their shape, `api.<cluster>.<environment domain>` and `*.apps.<cluster>.<environment
domain>`, in the environment's zone. For a cluster on a network they point to private addresses (the network's
floating IPs, or an AWS or Google Cloud cluster's internal load balancers). The hub's resolvers must return these
private answers: a resolver that drops private addresses in public answers (DNS rebinding protection) breaks the
install and the import.

## Reserved ranges, as tenants see them

The fabric computes every range a tenant prefix is refused in or stepped around into `HybridFabric
status.reservedRanges`; each tenant's profile copies the list of its fabric (`oc get tenantprofile shc -n
entity-<tenant> -o yaml`, `status.reservedRanges`, and `status.managedClusterRanges` for
`service.attachableRanges`). The dashboard shows both on the network pages.

| Source | Rule | Purpose shown to tenants |
|---|---|---|
| `infrastructure.hub` | Refused | Networks the platform's management connects from |
| `network.exclusiveRanges` | Refused | Address space the platform keeps for itself |
| `ipam.publicPools[].prefixes` | Refused | The platform's assignable IPv6 space |
| `core.srv6.block` | Refused | The platform's transport addresses |
| `core.icmpSource6`, `core.icmpSource4` | Refused | Addresses of the platform's network nodes |
| `core.loopbackPool`, `core.linkSubnet`, `attachments.linkPool`, `egress.linkPool` | Not allocated | Addresses inside the platform's network nodes |
| `placement.siteReserved` per site, the site's underlay | Not allocated, on that site | Addresses the site uses itself |

## What tenants and the platform team see

- The network's condition `PlatformReachable` and `status.platformAccess` (state, the hub's ranges and ports for the
  tenant's own firewalls, the cluster addresses and their state). False with `ServiceNotOffered` or
  `CircuitNotDeployed` is the platform team's: the domain is off, or no edge connection of the platform kind is
  realized on the fabric.
- The cluster's condition `Preflight`: refusals the tenant fixes, waits, and the platform team's
  (`ServiceNotOffered`, `CircuitNotDeployed`, `HubEndpointNotAdmitted`). A refusal creates nothing (no floating IP,
  DNS record or install). [Status reasons](../tenant/status-reasons.md) lists them.
- `HybridFabric status.realization.service`: the domain, its connections and VLANs, the networks with their
  accepted addresses, and the refused ones with their reason.
- The fabric health check reports `ServiceSessionsDown` when a node has no established session on a platform edge
  connection.

There is no live reachability probe: "the platform reaches this network" means the domain is offered, an edge
connection of the platform kind is realized and the network is eligible, not that packets were seen to flow. The
hub's routing (above) is the deployment's to verify.

## Checklist

1. `infrastructure.hub` lists every range the hub's management connects from, and nothing of a tenant's.
2. `service.enabled` with `attachments.enabled`; `service.ports`; `service.hubEndpoints` admitting the hub's API on
   6443 when it lies in the hub's ranges; `service.attachableRanges` as agreed with the hub's network team.
   `egress.platformSources` and `egress.platformPorts` removed.
3. A provider edge bearer with `ceKinds: [platform]` and at least one `AttachmentCircuit` with `ceKind: platform` in
   the fabric's namespace, Ready, with its sessions established from every node.
4. The hub's network peers with every node, accepts `advertisedRanges` with a fixed filter, announces the hub's
   ranges and routes the cluster addresses through the connections.
5. OpenStack sites: OVN 25.03 or later with `nat` in the EVPN router port's `dynamic-routing-redistribute`; the
   global external networks listed in the site's status.
6. The hub's resolvers return private answers for the environments' zones.
7. Before the upgrade that brings these settings: delete the tenant OpenStack clusters and the AWS or Google Cloud
   clusters that publish Internal.

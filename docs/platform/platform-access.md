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
  # the hub's addresses the service routing domain carries, inside infrastructure.hub (may be the same)
  range: [198.51.100.0/27]
  # TCP ports the hub may open toward a cluster's addresses
  ports: [6443, 443]
  # addresses in the service range the clusters may open TCP connections to (the hub's API for cluster management)
  hubEndpoints:
    - {prefix: 198.51.100.10/32, ports: [6443]}
  # the ranges a cluster's addresses may lie in (default: the private and shared blocks; 0.0.0.0/0: any)
  attachableRanges: [10.64.0.0/10]
  # what the hub's network routes itself: no cluster address there, tenant networks may use it
  hubRoutedRanges: [198.18.0.0/15, 203.0.113.0/24]
  # the most cluster addresses the platform announces to the hub's network
  maxPrefixes: 1000
```

- **`infrastructure.hub`**: every range the hub's management connects from, either family. No tenant address space,
  edge connection route or cluster network may overlap these ranges: a tenant prefix inside one is refused
  (`PrefixReserved`), and a realization pool that overlaps one fails the fabric (`InvalidSpec`, naming both keys).
  Networks created before the range was declared keep working; their `PlatformReachable` reads False
  `InfrastructureOverlap`. The traffic of Hive and ACM toward a cluster must leave the hub from addresses inside
  these ranges: when the hub's nodes translate their pods' traffic to the node addresses, the node network belongs
  here.
- **`service.enabled`** needs `attachments.enabled`, a non-empty IPv4 `infrastructure.hub` and `service.range`.
- **`service.range`** (required with `service.enabled`): the hub's IPv4 addresses the service routing domain
  carries, as prefixes (an address is written `/32`), each inside an IPv4 `infrastructure.hub` range. These are the
  only sources the border gateway nodes admit toward a cluster, the only destinations a cluster reaches through the
  domain, and the only routes the nodes accept from the hub's routers and offer to the tenant networks that host
  clusters. Keep it to what needs it: the addresses Hive and ACM connect from (the hub's node network when the hub's
  nodes translate their pods' traffic) and the hub endpoints. `infrastructure.hub` keeps its one meaning, the ranges
  tenants are refused; a deployment may set both to the same value. A missing value fails the fabric with
  `InvalidSpec` naming `service.range`.
- **`service.vrf`**: a name other than the egress VRF's and not of the form `e<digits>`; a table outside 253–255,
  not the egress table and below `egress.networkTableBase`.
- **`service.ports`**: what the hub may open toward a cluster's addresses. ICMP echo is admitted too.
- **`service.hubEndpoints`**: what a cluster may open toward the hub. Each prefix must lie inside
  `service.range`. Empty: the clusters reach the hub through their network's own outbound path (internet
  egress), and the hub's API address must then be reachable that way. When the hub's API address lies inside the
  service range and no entry admits it on 6443, a cluster on a network is refused before it is built
  (`HubEndpointNotAdmitted`), because cluster management could not reach the hub from it. With entries set, an
  OpenStack cluster's router gateway address joins the cluster's addresses after the install, so the nodes' own
  traffic to the endpoints is admitted.
- **`service.attachableRanges`** (default: the private and shared blocks 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16,
  100.64.0.0/10): IPv4 ranges a cluster's addresses may lie in, the reserved ranges taken out first (an entry may
  hold reserved ranges: a whole private block holding the hub's range, say). The hub's router is told exactly this
  list (`status.ceConfig`, `refusedRanges` before `advertisedRanges`), tenants see it as "Ranges for clusters managed
  by the platform", and a network with no IPv4 prefix inside it reads `PlatformReachable` False
  `OutsideAttachableRanges`. A deployment whose tenants run clusters on other space sets its own list. `0.0.0.0/0`
  is the one way to say any range the platform does not refuse; an empty list is refused by name. See
  [choosing](#the-default-the-allow-list-or-any-range).
- **`service.hubRoutedRanges`** (default none): the IPv4 ranges the hub's network routes itself (its servers, proxies,
  other sites, cloud-side subnets the fabric does not know, public ranges it reaches directly). No cluster address
  may lie in them, and the hub's routers refuse them from the platform; tenant networks may use them (each network
  is its own VRF, and a network is never announced to the hub's network, only a cluster's addresses are), existing
  networks included. Tenants see them as "not for managed clusters", and a cluster address there is refused with
  `NotForManagedClusters`. Do not use `network.exclusiveRanges` for this: that refuses tenant networks outright,
  existing ones included. `infrastructure.hub` stays the hub's own range, refused to tenants altogether.
- **`service.maxPrefixes`** (default 1000): the most cluster addresses the platform announces over a platform edge
  connection, given to the hub's network as its maximum-prefix figure. It is shared fairly and atomically: a
  cluster's addresses are admitted all or none, and an admitted cluster keeps its slot (below); the others take what
  is left in the order they came (a cluster that waits keeps its place in the order but holds no slot); the rest
  wait, with `ServiceLimitReached` on the network's row and the numbers in its message, and the fabric's message
  counts them.
- **`service.tenantShare`** (default 0.5): while two or more tenants ask, one tenant's clusters not yet admitted may
  not take it past this part of `service.maxPrefixes` (`ServiceShareReached`, waiting, with the numbers); a tenant
  alone may use all of it, and an admitted cluster is never pushed out by a newcomer. A tenant that filled the pool
  while it was alone keeps what it holds: the others wait until its clusters go (or the setting grows).
- **`service.admissionHoldMinutes`** (default 60): an admitted cluster keeps its slot from its admission; once its
  install has started (the cluster job tells its network at once) it keeps it for good; until then for this long,
  after which it counts as not admitted (the share applies to it again). A refused or deleted cluster gives its slot
  back at once.

### The default, the allow-list or any range

The hub's network learns each cluster's addresses from the platform as routes up to /32. A route that is more
specific than one the hub's network has wins there: a cluster address inside a range the hub's network routes (its
own servers, DNS, proxies, another site, or the internet behind its default route) would draw that traffic to the
tenant network instead of to the real destination. What keeps cluster addresses out of it:

- **Always, the refused ranges.** The platform refuses a cluster address that overlaps any range it publishes
  (`HybridFabric status.reservedRanges`), whatever the row's rule and whichever site it names, and the hub's routers
  refuse the same list from the platform at any length (`refusedRanges` below): `infrastructure.hub` and the service
  range, `network.exclusiveRanges`, the platform's own pools (`core.loopbackPool`, `core.linkSubnet`,
  `core.icmpSource4`, `attachments.linkPool`, `egress.linkPool`), the internet hand-off (each egress node's NAT pool
  and its hand-off network), every site's underlay and its `placement.siteReserved` ranges, and the special-purpose
  space (0.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4, 240.0.0.0/4, and the default route itself), and
  `service.hubRoutedRanges`. The hand-off, the special-purpose space and `service.hubRoutedRanges` are published with
  the rule `NotForManagedClusters`: they constrain a cluster's addresses only, never a tenant network or a cluster's
  own pod, service and machine networks. A site's
  own ranges count for every cluster, on every site: a cluster address is announced to the hub's network, which
  routes every site. (A tenant network is judged differently: a site's own ranges only constrain its placements on
  that site.)
- **The default allow-list**, the private and shared blocks: no cluster address in public space, so a hub that
  reaches the internet by a default route never prefers a cluster's address over the internet's. The private ranges
  the hub's network routes itself still need declaring, in `service.hubRoutedRanges`.
- **Your own allow-list**: ranges agreed with the hub's network team that it does not use itself. Choose it when the
  hub's network cannot list all it routes (a large or shared enterprise network), or when its team wants an import
  filter that does not change with the platform's reserved ranges.
- **Any range** (`[0.0.0.0/0]`): tenants number networks as they like, public space included. The hub's routers then
  accept any /32 from the platform: where the hub reaches the internet by a default route, a cluster's address is
  preferred over the internet's (the hub's traffic to that internet address goes to the tenant), so choose it only
  for a hub without a default route, and declare everything its network routes in `service.hubRoutedRanges`.

Either way the platform refuses what lies inside a reserved range, and either way the hub's routers should keep the
refusal as a second guard.

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

An edge connection of the platform kind names no network and accepts no routes of its own: it accepts routes inside
the service range only and announces the clusters' addresses. It lives in the fabric's namespace (tenants cannot write there, and
the tenant dashboard refuses the platform kind); it is refused when the service domain is off
(`ServiceNotOffered`) and for IPv6 (`FamilyUnavailable`). Two connections (two hub routers, or two bearers) give
redundancy; all of them sit in the service domain.

The platform's operator realizes these connections in the fabric's namespace and only of `ceKind: platform`: any
other circuit created there is refused at once (`InvalidSpec`, phase Failed, nothing realized; delete it), because a
tenant network's connection (`ceKind: customer`) belongs in the tenant's namespace.

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
addresses and session per border gateway node, and its filters in the order it applies them:

| Key | The hub-side router |
|---|---|
| `acceptedRanges` | announces these to the platform (`service.range`, up to /32); the platform accepts nothing else |
| `refusedRanges` | refuses these from the platform: the default route itself (`0.0.0.0/0`, exact), then each published reserved range at any length (IPv4, every site's own included) |
| `advertisedRanges` | then accepts these from the platform: each `service.attachableRanges` entry up to /32 (`0.0.0.0/0 le 32` when the list is `0.0.0.0/0`) |
| `maxPrefixes` | sets this as its maximum-prefix for the session (`service.maxPrefixes`) |

`status.sessions` shows each node's session.

## Cluster builds on the hub

Every OpenShift cluster the platform builds has a build name (`status.buildName`, unique per tenant and cluster) and
a namespace of that name on the hub, which its first run claims with the label
`hybridsovereign.redhat/platformopenshift-uid` (the uid of the PlatformOpenshift). A cluster whose build name is held
by a namespace another object claimed is refused (`NamespaceTaken`), and a teardown deletes only the hub objects of
the build its own cluster claimed. Do not remove or copy that label by hand, except as below.

A build belongs to the object by its uid only. A PlatformOpenshift restored from a backup or moved (re-created with
its status) gets a new uid, so the platform no longer treats the build it recorded as its own: its next run refuses
with `BuildOwnerMismatch` (condition `Preflight`, the message names the hub namespace, the uid on it and the
object's), keeps its record and changes nothing; deleting it removes nothing on the hub and leaves a Warning event
`BuildOwnerMismatch` on the object. Nothing is adopted automatically. After such a restore:
1. Check that the build is this cluster's: the namespace `status.buildName` on the hub, its annotation
   `hybridsovereign.redhat/owner` (`PlatformOpenshift <namespace>/<name>`) and the old uid on it, and that no other
   PlatformOpenshift has that uid any more (`oc get platformopenshift -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,UID:.metadata.uid`).
2. Hand the build to the restored object:
   `oc label namespace <build name> hybridsovereign.redhat/platformopenshift-uid=<new uid> --overwrite` (on the hub).
3. Retry the object (or delete it, to tear the build down). Delete a restored object only after step 2: its teardown
   otherwise removes its recorded floating IPs and DNS records but leaves the build running on the hub.

A cluster that never recorded a build name owns nothing on the hub: deleting it removes nothing there and leaves a
Warning event `NoBuildRecorded` on the object. A build an older job made for it before build names were recorded (a
hub namespace named after its tenant and name) is left for the platform team to find and remove.

## What the hub's network must do

The hub's network must route the tenant cluster addresses through the platform's edge connections. On the
hub-side router:

1. A VLAN interface on the bearer's link for each connection, with the router's link address from `status.ceConfig`.
2. An eBGP session with every border gateway node listed in `status.ceConfig` (password, timers and BFD as the
   connection says).
3. Import, in this order: deny `refusedRanges` (each entry as written, at any length), then permit
   `advertisedRanges`, deny the rest; `maximum-prefix` `maxPrefixes`. The platform never announces anything else,
   and the refusal stays a second guard if a reserved range is added later.
4. Export: announce `acceptedRanges` (the service range); the platform accepts nothing else.
5. Route the learned cluster addresses toward the connections from the addresses in the service range, so that the
   hub's traffic to a cluster (Hive's install, ACM's import and management) takes them, and leaves from those
   addresses.

What the platform admits through the domain, by destination on the tenant side: from the hub's routers, only
sources inside `service.range`, only to the cluster addresses it accepted, TCP on `service.ports` and ICMP echo;
from a tenant network, only packets to `service.range`: replies from the service ports, connections to
`service.hubEndpoints`, ICMP errors and echo replies, and only with a source the domain routes back to that network
(its accepted cluster addresses). No tenant network reaches another through the domain, and no route crosses from it
into a tenant network beyond the service range, which a network learns only while it hosts a cluster. The filter is
stateless (the two directions of a connection may cross different nodes); what that leaves open (reply-shaped
segments, resets at a limited rate) is bounded by the service range.

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
`service.attachableRanges`, `0.0.0.0/0` for any range not reserved, absent where not offered). The dashboard shows both
on the network pages.

| Source | Rule | Purpose shown to tenants |
|---|---|---|
| `infrastructure.hub` | Refused | Networks the platform's management connects from |
| `network.exclusiveRanges` | Refused | Address space the platform keeps for itself |
| `ipam.publicPools[].prefixes` | Refused | The platform's assignable IPv6 space |
| `core.srv6.block` | Refused | The platform's transport addresses |
| `core.icmpSource6`, `core.icmpSource4` | Refused | Addresses of the platform's network nodes |
| `core.loopbackPool`, `core.linkSubnet`, `attachments.linkPool`, `egress.linkPool` | Not allocated | Addresses inside the platform's network nodes |
| `placement.siteReserved` per site, the site's underlay | Not allocated, on that site | Addresses the site uses itself |
| `service.hubRoutedRanges` | Not for managed clusters | Addresses the platform's management network routes |
| each egress node's NAT pool and hand-off network | Not for managed clusters | The platform's internet hand-off |
| 0.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4, 240.0.0.0/4 | Not for managed clusters | Special-purpose addresses that are never routed |

## What tenants and the platform team see

- The network's condition `PlatformReachable` and `status.platformAccess` (state, the service range and ports for
  the tenant's own firewalls (`sources`: the only addresses the platform's management connects from), the cluster
  addresses and their state). False with `ServiceNotOffered` or
  `CircuitNotDeployed` is the platform team's: the domain is off, or no edge connection of the platform kind is
  realized on the fabric.
- The cluster's condition `Preflight`: refusals the tenant fixes, waits, and the platform team's
  (`ServiceNotOffered`, `CircuitNotDeployed`, `HubEndpointNotAdmitted`). A refusal creates nothing (no floating IP,
  DNS record or install). [Status reasons](../tenant/status-reasons.md) lists them.
- `HybridFabric status.realization.service`: the domain, its connections and VLANs, the networks with their
  accepted addresses, and the refused ones with their reason.
- The fabric health check reports `ServiceSessionsDown` when a node has no established session on a platform edge
  connection, and `ServiceLinkDown` when, on a node whose edge session is up, a network's session with the service
  domain is down (that network learns the service range through the other nodes only).

There is no live reachability probe: "the platform reaches this network" means the domain is offered, an edge
connection of the platform kind is realized and the network is eligible, not that packets were seen to flow. The
hub's routing (above) is the deployment's to verify.

## Checklist

1. `infrastructure.hub` lists every range the hub's management connects from, and nothing of a tenant's.
2. `service.enabled` with `attachments.enabled`; `service.range` (the hub's addresses Hive, ACM and the hub
   endpoints use); `service.ports`; `service.hubEndpoints` admitting the hub's API on 6443 when it lies in the
   service range; either the refused ranges covering everything the hub's network routes, or
   `service.attachableRanges` (the default private and shared blocks, or a list agreed with the hub's network team;
   `0.0.0.0/0` only for a hub without a default route); `service.maxPrefixes`.
   `egress.platformSources` and `egress.platformPorts` removed.
3. A provider edge bearer with `ceKinds: [platform]` and at least one `AttachmentCircuit` with `ceKind: platform` in
   the fabric's namespace, Ready, with its sessions established from every node.
4. The hub's network peers with every node, refuses `refusedRanges`, accepts `advertisedRanges`, sets
   `maxPrefixes`, announces `acceptedRanges` and routes the cluster addresses through the connections.
5. OpenStack sites: OVN 25.03 or later with `nat` in the EVPN router port's `dynamic-routing-redistribute`; the
   global external networks listed in the site's status.
6. The hub's resolvers return private answers for the environments' zones.
7. Before the upgrade that brings these settings: delete the tenant OpenStack clusters and the AWS or Google Cloud
   clusters that publish Internal.

## Upgrading to the service range

- **The service off**: nothing changes and nothing new is required (`service.range` and `service.maxPrefixes` are
  read only with the service on).
- **The service on, `attachableRanges` set**: keeps working once `service.range` is set; the allow-list restricts as
  before.
- **The service on, no `service.range`** (or any other wrong `service.*` setting): nothing that runs is torn down.
  The fabric keeps each node's service as it was last applied (each node step reads the node's configuration and passes it on
  marked as kept: the node applies no service step while it holds the service, so the filter, the links and the
  sessions stay exactly as they are, the previous release's filter by `infrastructure.hub` included; a node that
  lost it, at a reboot for one, rebuilds it from that block, which survives in its configuration file; a node with
  no block to keep, rebuilt or new, has no service until the setting is fixed, and the condition and the node's
  health finding `ServiceSessionsDown` name it). Meanwhile nothing that needs the service planned again moves: a
  network being deleted waits (its own status says `ServiceSettingInvalid` and names the setting), a cluster's new
  addresses wait and a deleted cluster's addresses stay announced toward its own network until the setting is fixed, keeps the last realized status, carries no
  new cluster address, and goes on with everything else (networks, egress, edge connections). It is loud: the
  fabric's condition `ServiceRouting` is False `InvalidSpec` naming the setting, the alert
  `ShcFabricServiceSettingInvalid` fires, a Warning event `ServiceSettingInvalid` is written once per change and the
  fabric's Ready message repeats it. A service never applied is not realized (`ServiceNotRealized`, the networks and
  edge connections `ServiceNotOffered`, naming the setting). Only `service.enabled: false` removes the service. Set
  `service.range` in the installation's `FABRIC_REALIZATION` to the hub's addresses that reach the clusters (at
  most `infrastructure.hub`), move every `service.hubEndpoints` entry inside it, and let
  the fabric run again. Then give the hub's network team the connections' new `status.ceConfig`: the routes they
  announce shrink to `acceptedRanges`, and they add `refusedRanges` and `maxPrefixes` to their import filter.

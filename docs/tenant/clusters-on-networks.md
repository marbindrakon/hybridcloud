# OpenShift clusters on your networks

An OpenShift cluster that the platform builds for you can sit on one of your networks: its API and applications
then have addresses of that network and are reachable from your other places on the network, and from the
platform, which installs and manages the cluster. This page says what to set per cloud, what the platform needs on
the network, which address ranges you cannot use, and what each refusal means.

## OpenStack

An OpenStack cluster keeps its own machine network behind its own router. Its router's external gateway and its
two floating IPs (one for the API, one for the applications) are on the network you choose:

- **One of your networks.** Attach the network to the OpenStack environment and turn on **Use for router
  attachments** on that network placement. The cluster's API and applications are then reachable from the network
  and from the platform only.
- **An external network of the site.** The networks every project of the site may use, listed in your tenant
  profile for each OpenStack site (`status.sites[].externalNetworks`). The site's default is preselected in the
  form. The cluster is reachable as that network is (often from the internet).

A network placement on an OpenStack environment has two settings, which you can change at any time:

| Setting | Default | What it does |
|---|---|---|
| Use for virtual machines | on | Virtual machines and ports of the environment attach directly to the network. |
| Use for router attachments | off | Routers of the environment may use the network as their external network, with floating IPs from it. Needed by an OpenShift cluster on the network. |

A placement needs at least one of them. For a network that only hosts clusters, turn **Use for virtual machines**
off. Turning a use off waits while the environment still uses the network that way; the placement page lists what
still uses it (a router gateway, a floating IP, a port of a server).

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: NetworkPlacement
metadata:
  name: example-net-oso
  namespace: entity-example-corp
spec:
  network: example-net
  backend:
    kind: CloudOSO
    name: example-oso
  ipv4:
    prefixLength: 24
  use:
    virtualMachines: false
    routerAttachments: true
---
apiVersion: hybridsovereign.redhat/v1alpha1
kind: PlatformOpenshift
metadata:
  name: example-ocp
  namespace: entity-example-corp
spec:
  type: openstack
  openstack:
    environment: example-oso
    network: example-net
    controlPlaneCount: 3
    workerCount: 0
```

`network` and `externalNetwork` exclude each other and cannot be changed once the cluster exists. With neither,
the site's default external network applies; a site without one refuses the cluster.

## AWS and Google Cloud

A cluster on AWS or Google Cloud can name a network attached to its environment (`spec.aws.network`,
`spec.gcp.network`); its machines are then on the placement's subnet. **Publish** says how the cluster is reached:

- **External**: public load balancers. No platform access to the network is needed.
- **Internal**: the API and applications get addresses of the network only. This needs the platform's access to
  the network (below).

Without a network the cluster gets a network of its own and publishes External. The region is the environment's.

## Platform access

Every network allows platform access unless you turn it off (**Allow platform access**, `spec.platformAccess`, on
by default). It lets the platform's management open connections into the network, only to the API and application
addresses of the clusters it builds there and only on its service ports, and lets those clusters reach the
platform's service endpoints. Nothing is reserved or routed for the network until a cluster is built on it, and
no other network is reached this way. The network page shows the platform's own addresses and ports, for your own
firewalls (the only addresses the platform's management connects from, and the only ones your clusters reach
through this access), and the cluster addresses it reaches.

The network's **PlatformReachable** condition says whether the platform reaches the network:

| Reason | What it means |
|---|---|
| `Attachable`, `Attached` | The platform reaches the network (Attached: a cluster is on it). |
| `NotRequested` | Platform access is off on the network. Turn it on (the cluster form offers **Allow platform access**). |
| `NoIPv4` | The network has no IPv4 address space; clusters need IPv4. |
| `OutsideAttachableRanges` | The platform names ranges for clusters it manages, and no IPv4 prefix of the network lies in them. |
| `InfrastructureOverlap` | The network's addresses overlap a range the platform reserves; the platform cannot reach it. |
| `ServiceNotOffered`, `CircuitNotDeployed` | The platform's connection for clusters on networks is not ready. Ask the platform team. |
| `AttachPending` | The platform is connecting to the network; this takes a few minutes. |

Turning platform access off while a cluster is on the network waits until the cluster is deleted.

## Address ranges reserved by the platform

Your tenant profile lists the ranges the platform reserves (`oc get tenantprofile shc -n entity-<tenant> -o yaml`,
`status.reservedRanges`), and the network pages show them under **Address ranges reserved by the platform**:

- **Refused**: no network, edge connection route or cluster range of yours may overlap it. The network form marks
  a prefix inside one before you create the network.
- **Not allocated**: the platform does not give subnets from it (on the site named, where a row names one).

**Ranges for clusters managed by the platform** (`status.managedClusterRanges`): where the platform names them, a
network must have addresses in them to host clusters the platform manages (on OpenStack the floating IPs, on AWS
and Google Cloud with Internal the placement's subnet). Where it names none (the list is absent, or `0.0.0.0/0`),
**any range** will do that is not refused above: you number your networks as you like, and a cluster's addresses
are taken first come, first served, so two tenants with the same address space can both have clusters as long as
their clusters' addresses differ (`ServicePrefixInUse` tells the second one; creating the cluster again picks new
addresses). Whether the platform offers clusters it manages on a network at all shows in the network's
`PlatformReachable` condition (`ServiceNotOffered` when it does not).

## Address settings of a cluster

Under **Address settings** in the form (`spec.networking`, fixed once the cluster exists), each empty field is the
installation's default (OpenShift's defaults are shown as examples):

| Setting | Field | Rule |
|---|---|---|
| Pod network | `clusterNetwork` | IPv4 /8 to /22 |
| Pod subnet size per node | `hostPrefix` | 16 to 28, longer than the pod network's prefix |
| Service network | `serviceNetwork` | IPv4 /12 to /24 |
| Machine network | `machineNetwork` | IPv4 /16 to /27; not with an AWS or Google Cloud network (the placement's subnet is the machine network) |

The pod and service networks must not overlap each other, the machine network, the network's address space or a
reserved range; the machine network must not overlap the network's address space or a reserved range. The form
checks this before you create the cluster and names both ranges; when an empty field's OpenShift default would
collide, it says which field to set.

## Node counts

The form sends what it shows: an empty field is the installation's default, 0 workers builds a compact cluster
(the control plane nodes also run applications), and the control plane needs at least 1 node (usually 3).

## When a cluster is refused

The cluster's **Preflight** condition says why it was not built; nothing was created (no floating IP, DNS record or
install). [Status reasons](status-reasons.md) lists every reason and who acts.

| Reason | What to do |
|---|---|
| `RouterAttachmentsOff` | Turn on **Use for router attachments** on the network's placement in this environment. |
| `WaitingForPlacement` | The placement is not ready yet; the cluster waits. |
| `PlacementHasNoIPv4` | The placement has no IPv4 subnet; clusters need one. |
| `PlatformAccessOff` | Allow platform access on the network. |
| `OutsideAttachableRanges` | Use a network (placement subnet) inside the ranges for clusters managed by the platform (only where the platform names such ranges). |
| `AddressConflict` | Two of the cluster's ranges, or a range and the network's space, overlap; the message names both. |
| `PrefixReserved` | A range overlaps a range the platform reserves; the message names it and its purpose. |
| `ServicePrefixInUse` | An address of the cluster is already reached by the platform for another network. Create the cluster again (new addresses are chosen) or ask the platform team. |
| `NetworkChoiceRequired` | The site has no default external network: choose a network. |
| `ExternalNetworkNotListed` | The external network named is not one of the site's. |
| `NetworkNameNotUnique` | The environment sees another network with the same name as the cluster's network, and the installer finds the network by its name. Rename or remove the other network; a network shared with the environment by someone else: ask the platform team. |
| `WaitingForPlatformAccess` | The platform is connecting to the network; the cluster waits. |
| `ServiceNotOffered`, `CircuitNotDeployed`, `HubEndpointNotAdmitted` | The platform's connection for clusters on networks is not ready. Ask the platform team. |

A cluster on a network whose install never reaches the cluster ends Failed with a message that the platform could
not reach the cluster at its API address; the platform team checks the platform's connection to the network and
that the site advertises floating IPs ([platform access](../platform/platform-access.md)).

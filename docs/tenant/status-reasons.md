# Status, Retry now and deletion

Every object you create shows what the platform is doing with it: one **state**, a one-line
**summary**, and a **reason** that you can look up below (the reasons of a Ready object, such as
`EDASucceeded` or `GroupClaimVerified`, only say that the work finished and are not listed). This page explains them, how to ask
the platform to try again, and how deleting works.

## States

| State | Meaning |
|---|---|
| **Waiting** | The object waits for something else, for example the environment a placement uses. |
| **Creating** | The platform is creating it for the first time. |
| **Updating** | The platform is applying your latest change. |
| **Ready** | Done; the object is usable. A routine re-check keeps it Ready. |
| **Degraded** | Usable, with reduced health. The summary says what is wrong. |
| **Paused** | The platform team has paused the object's site (for maintenance, or while the site is powered off). The object keeps its last state; nothing on the site changes until it is resumed, and a deletion waits until then. |
| **Failed** | It could not be completed. The reason says who has to act. |
| **Deleting** | It is being deleted, or its deletion waits for something. |

On the command line:

```bash
oc get hybridnetworks,cloudvirts,networkplacements -n entity-<tenant>   # STATUS, READY, REASON
oc get hybridnetworks,cloudvirts,networkplacements -n entity-<tenant> -o wide   # adds SUMMARY
oc get networkplacement <name> -n entity-<tenant> -o jsonpath='{.status.phase}: {.status.summary}{"\n"}'
oc describe networkplacement <name> -n entity-<tenant>                  # all conditions
```

The state is in `status.phase` (since `status.phaseSince`) and is what the **STATUS** column of
`oc get` and the **Status** column of the dashboard lists show. The summary is in
`status.summary` (the **SUMMARY** column with `-o wide`), and the reason is the `reason` of the
`Ready` condition (`status.conditions`, the **REASON** column). `status.message` has more detail
where the platform wrote one. `oc explain networkplacement.status` describes each of these fields.

## Retry now

The platform retries failed work by itself. When you have fixed what a failure named (or want an
object processed again at once), use **Retry now** on the object's page. While the request is
open the page shows **Retry queued**.

On the command line, write the current time into the annotation
`ansible.sdk.operatorframework.io/reconcileNow`:

```bash
oc annotate hybridnetwork <name> -n entity-<tenant> \
  ansible.sdk.operatorframework.io/reconcileNow="$(date -u +%FT%TZ)" --overwrite
```

Every new value is a new request. The platform records the value it handled in
`status.lastHandledReconcileRequest` and leaves the annotation in place, so the request is open
while the two differ. Always write a new value (the current time): setting the same value again
does nothing. The older form `reconcileNow="true"` still works; the platform removes that
annotation once it has handled it.

## Deleting

You can delete each object yourself; delete the ones that use others first: placements and edge
connections, then clusters, then environments, then networks. A delete that has to wait shows
**Deleting** and says what it waits for.

When a tenant is deleted, the platform deletes its objects in this order, and each step waits
for the one before it:

| Step | Objects |
|---|---|
| 1 | cluster assignments, edge connections, network placements, VM migrations to OpenStack |
| 2 | OpenShift clusters |
| 3 | environments (OpenShift Virtualization, OpenStack, AWS, Google Cloud) |
| 4 | networks |
| 5 | team setups, app projects, AAP and Quay organizations, KV secrets engines |
| 6 | Vaults |
| 7 | access groups |

The tenant profile goes with the tenant namespace.

**Deletion protection.** Environments, networks and OpenShift clusters have
`spec.deletionProtection`. When it is `true`, deleting the tenant does not delete the object and
the tenant's deletion waits (reason `ProtectedChildren`) until protection is turned off. It does
not stop you from deleting the object itself. The platform team sees the objects a tenant
deletion waits for in the tenant's `status.deletionBlockedBy`.

A tenant deletion also waits with reason `UnknownKind` when the tenant namespace holds an object
the platform does not know how to delete; the platform team resolves that.

## Reasons

Who acts: **You** can fix it (the summary says how), **the platform team** has to act (they are
alerted or you tell them), or **nobody**: the platform continues by itself. Reasons that only mean
"ready" are not listed.

The last group lists reasons shown on a part of an object, such as one address family's internet
access on a network page (`status.egress.families`). They explain that part and never change the
object's state: a network whose IPv6 internet access is not offered is still Ready.

<!-- reasons:begin -->
**Working on it**

| Reason | What it means | Who acts |
|---|---|---|
| `AwaitingEDA` | Queued; the change starts shortly. | Nobody: it continues by itself |
| `ClusterBuildCreated` | The cluster build has started. | Nobody: it continues by itself |
| `ClusterBuildProvisioned` | The cluster is being installed. | Nobody: it continues by itself |
| `Deleted` | Deleted; being removed. | Nobody: it continues by itself |
| `Deleting` | Being deleted. | Nobody: it continues by itself |
| `DeletionRequested` | Being deleted. | Nobody: it continues by itself |
| `InProgress` | Applying the change. | Nobody: it continues by itself |
| `JobRunning` | Applying the change. | Nobody: it continues by itself |
| `NotReady` | Not ready yet. | Nobody: it continues by itself |
| `Published` | Queued; the change starts shortly. | Nobody: it continues by itself |
| `Publishing` | Queued; the change starts shortly. | Nobody: it continues by itself |
| `Queued` | Queued; the change starts shortly. | Nobody: it continues by itself |

**Waiting**

| Reason | What it means | Who acts |
|---|---|---|
| `CredentialsUnavailable` | The site credentials are not available yet. The platform team is looking at it. | The platform team |
| `InUse` | A setting turned off waits until the environment no longer uses it; the message lists what still does. | You (the tenant) |
| `LinkedNotReady` | Waiting for its team setup, app projects and OpenShift cluster to be ready. | Nobody: it continues by itself |
| `NoMembersYet` | Ready; sign-in will be verified once a granted access group has a member. | You (the tenant) |
| `ReferenceDeleting` | An object this one refers to is being deleted. | You (the tenant) |
| `Waiting` | Waiting for a prerequisite. | Nobody: it continues by itself |
| `WaitingForAccessGroup` | Waiting for the named access groups to be ready. | You (the tenant) |
| `WaitingForAddressSpace` | Waiting for free address space. The message says what is full; adding a prefix or freeing a block lets it continue. | You (the tenant) |
| `WaitingForCluster` | Waiting for a cluster: name the OpenShift cluster to deploy onto. | You (the tenant) |
| `WaitingForDependencies` | Waiting for the objects this one depends on. | Nobody: it continues by itself |
| `WaitingForDependents` | Waiting for the objects that depend on this one to be deleted. | Nobody: it continues by itself |
| `WaitingForFabric` | Waiting for the network fabric to be ready. | Nobody: it continues by itself |
| `WaitingForGateway` | Waiting for the site connection to be ready. | Nobody: it continues by itself |
| `WaitingForPlacement` | Waiting for the network's placement in this environment to be ready. | Nobody: it continues by itself |
| `WaitingForPlatformAccess` | The platform is connecting to the cluster's network; this takes a few minutes. | Nobody: it continues by itself |
| `WaitingForSecret` | Waiting for a required credential to be available. | Nobody: it continues by itself |
| `WaitingForSiteEdge` | Waiting for the site connection to be ready. | Nobody: it continues by itself |

**Blocked**

| Reason | What it means | Who acts |
|---|---|---|
| `ControllerOutOfMemory` | Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost. | The platform team |
| `ControllerRestarting` | Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost. | The platform team |
| `ControllerUnavailable` | Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost. | The platform team |
| `NoJobStarted` | Changes in this tenant are delayed. The platform team is alerted; nothing you created is lost. | The platform team |
| `ProtectedChildren` | Deletion waits: some objects have deletion protection turned on. | You (the tenant) |
| `UnknownKind` | Deletion waits: an object of an unknown kind is in the tenant. The platform team is looking at it. | The platform team |

**Paused**

| Reason | What it means | Who acts |
|---|---|---|
| `Paused` | Paused by the platform team; nothing changes on this site until it is resumed. | The platform team |
| `SitePaused` | Held: its site is paused. It keeps its last state until the site is resumed. | Nobody: it continues by itself |

**Failed**

| Reason | What it means | Who acts |
|---|---|---|
| `AccessNotMapped` | No access group named here exists in this tenant; nobody can sign in yet. | You (the tenant) |
| `AddressConflict` | The cluster's address ranges overlap each other or the network's addresses; the message names both. | You (the tenant) |
| `AsnConflict` | The AS number conflicts with one already in use. | You (the tenant) |
| `AttachmentNotSupported` | This attachment is not supported on the chosen site. | You (the tenant) |
| `BearerNotFound` | The provider edge has no bearer of that name. Ask the platform team for the bearer names. | You (the tenant) |
| `BuildOwnerMismatch` | The cluster's build on the platform is marked for another copy of this cluster (a restored copy). The platform team checks it. | The platform team |
| `CeKindNotAccepted` | The bearer does not accept this kind of router. Choose another bearer or ask the platform team. | You (the tenant) |
| `CircuitNotDeployed` | The platform's connection for clusters on networks is not ready. Ask the platform team. | The platform team |
| `ClaimMissing` | Members of the granted access groups cannot sign in yet. The platform team is looking at it. | The platform team |
| `CnvNotAvailable` | The site does not offer virtual machines yet. The platform team is looking at it. | The platform team |
| `ConfigNotFound` | The named connection does not exist. | You (the tenant) |
| `CredentialsInvalid` | The site credentials were refused. The platform team is looking at it. | The platform team |
| `CredentialsMissing` | The site credentials are missing. The platform team is looking at it. | The platform team |
| `DnsProjectNotFound` | The DNS project does not exist. The platform team is looking at it. | The platform team |
| `EDAFailed` | Could not be completed; see the reason. | The platform team |
| `EntityNotAllowed` | This site is not available to this tenant. | You (the tenant) |
| `EnvironmentNameTaken` | This environment name is already in use. Choose another name. | You (the tenant) |
| `ExternalNetworkNotFound` | The site's external network was not found. The platform team is looking at it. | The platform team |
| `ExternalNetworkNotListed` | The named network is not an external network of this site. | You (the tenant) |
| `FabricAmbiguous` | More than one fabric could serve this request. The platform team must resolve it. | The platform team |
| `FabricMismatch` | The network and the site belong to different fabrics. | You (the tenant) |
| `Failed` | Could not be completed; see the reason. | The platform team |
| `FamilyUnavailable` | This address family or IPv6 option is not offered here. The message says whether the site or the network lacks it. | You (the tenant) |
| `HubEndpointNotAdmitted` | The platform's connection for clusters on networks is not ready. Ask the platform team. | The platform team |
| `IPv6ModeInUse` | The network's IPv6 mode can't change while it has placements; remove them or create a new network. | You (the tenant) |
| `InvalidSecret` | A required credential is not valid. The platform team is looking at it. | The platform team |
| `InvalidSpec` | The request is not valid; correct the fields named in the message. | You (the tenant) |
| `MapperMissing` | Sign-in is not set up for this service yet. The platform team is looking at it. | The platform team |
| `MappingFailed` | The sign-in mapping could not be set up. The platform team is looking at it. | The platform team |
| `NameInUse` | This name is already used by an OpenStack environment of another tenant. Create it with another name. | You (the tenant) |
| `NamespaceNotOwned` | The tenant namespace already exists and was not created for this tenant. The platform team must remove or rename it. | The platform team |
| `NamespaceOperatorImageRejected` | The requested operator version for this tenant is not allowed; the default version is used. | The platform team |
| `NamespaceTaken` | The namespace name is already in use on the site. Choose another name. | You (the tenant) |
| `NetworkChoiceRequired` | This site has no default external network. Choose a network. | You (the tenant) |
| `NetworkNameNotUnique` | Your environment sees another network with the name of the cluster's network. Rename or remove the other network. | You (the tenant) |
| `NetworkNotFound` | The cluster's network or its placement in this environment was not found. | You (the tenant) |
| `NoAccount` | No cloud account is set up for this environment. | You (the tenant) |
| `NoBuildRecorded` | The cluster recorded no build on the platform, so its deletion removed nothing there. The platform team checks. | The platform team |
| `NoDefaultConfig` | No default connection is configured for this service. Ask the platform team. | The platform team |
| `NoProject` | No cloud project is set up for this environment. | You (the tenant) |
| `NodeUnreachable` | A network node does not answer. Traffic takes the other nodes; the platform team is alerted. | The platform team |
| `NotForManagedClusters` | An address of this cluster lies in a range the platform's management network routes. Use another network or address. | You (the tenant) |
| `NotPlatformNamespace` | Platform circuits are accepted only in the platform's namespace. Connect a network with ceKind customer. | You (the tenant) |
| `NotRealizable` | The request cannot be realized on the chosen site. | You (the tenant) |
| `OperatorFailed` | The request could not be queued. The platform team is looking at it. | The platform team |
| `OutsideAttachableRanges` | Clusters the platform manages need a network inside the ranges your tenant profile lists (managedClusterRanges). | You (the tenant) |
| `PeerAsnReserved` | This AS number is reserved by the platform. Configure your router with another AS number. | You (the tenant) |
| `PlacementHasNoIPv4` | The network has no IPv4 subnet here; clusters need one. | You (the tenant) |
| `PlatformAccessOff` | The platform may not reach this network. Allow platform access on the network. | You (the tenant) |
| `PolicyRefused` | The request was refused by a platform policy. | You (the tenant) |
| `PrefixInUse` | The address prefix overlaps one already in use: a placement's subnet or another network of this tenant. | You (the tenant) |
| `PrefixInvalid` | The address prefix is not valid. | You (the tenant) |
| `PrefixReserved` | The address prefix overlaps address space the platform keeps for itself. Choose another prefix. | You (the tenant) |
| `ProbeFailed` | The site check failed. The platform team is looking at it. | The platform team |
| `ProjectDomainNotFound` | The referenced project or domain does not exist. | You (the tenant) |
| `ProvisionFailed` | Creating or updating did not complete. The platform team is looking at it. | The platform team |
| `PublishFailed` | The request could not be queued. The platform team is looking at it. | The platform team |
| `RegionNotSupported` | This region is not supported. | You (the tenant) |
| `RouterAdvertisementFailed` | The site did not confirm that floating IPs on this network are reachable from outside it. The platform team has to fix this. | The platform team |
| `RouterAttachmentsOff` | The network's placement here does not allow router attachments. Turn on 'Use for router attachments' on the placement. | You (the tenant) |
| `RoutesMissing` | Network routes are missing. The platform team is looking at it. | The platform team |
| `RoutingDaemonDown` | Routing on a node is partly down. The platform team is alerted. | The platform team |
| `ServiceFailed` | A network service on a node is not running. The platform team is alerted. | The platform team |
| `ServiceLinkDown` | On a node, a network lacks the platform's routes for its clusters; other nodes carry them. The platform team is alerted. | The platform team |
| `ServiceNotOffered` | The platform's connection for clusters on networks is not offered here. Ask the platform team. | The platform team |
| `ServicePrefixInUse` | An address of this cluster is already reached by the platform for another network. Try again or ask the platform team. | You (the tenant) |
| `ServiceSessionsDown` | A node has no session with the platform's router for clusters on networks. The platform team is alerted. | The platform team |
| `SiteNotAllowed` | This site is not available to this tenant. | You (the tenant) |
| `SiteNotEvpnCapable` | The chosen site cannot join hybrid networks. | You (the tenant) |
| `SiteNotVirtualizationCapable` | The chosen site does not offer virtual machines. | You (the tenant) |
| `SiteSessionsDown` | The site's routing sessions are down on a node. The platform team is alerted. | The platform team |
| `SiteTypeMismatch` | This kind of environment cannot be created on the chosen site. | You (the tenant) |
| `SiteUnreachable` | The site does not answer. The platform team is looking at it. | The platform team |
| `StartFailed` | The network configuration did not start on a node. The platform team is alerted. | The platform team |
| `StartIncomplete` | The network configuration was applied in part on a node. The platform team is alerted. | The platform team |
| `TeardownFailed` | Deleting did not complete. The platform team is looking at it. | The platform team |
| `TransportNotSupported` | This transport is not supported. | You (the tenant) |
| `TypeNotSupported` | This type is not supported here. | You (the tenant) |
| `ZoneMismatch` | The zone does not match the site. | You (the tenant) |

**On a part of an object (an egress family of a network, its translation to IPv4, a placement's IPv6 internet access)**

| Reason | What it means | Who acts |
|---|---|---|
| `AddressChoice` | Not offered for tenant-provided IPv6 prefixes: IPv6 internet access, or translation to IPv4. | You (the tenant) |
| `AttachPending` | The platform is connecting to this network; try again in a few minutes. | Nobody: it continues by itself |
| `Attachable` | Clusters the platform manages can be built on this network. | Nobody: it continues by itself |
| `Attached` | The platform reaches the clusters it manages on this network. | Nobody: it continues by itself |
| `CircuitNotDeployed` | The platform's connection for clusters on networks is not ready. Ask the platform team. | The platform team |
| `ConfigurationDrift` | A node's network state differs from its configuration; a repair run is requested. | Nobody: it continues by itself |
| `ConfigurationNotTaken` | A node's routing does not take part of its configuration; the platform team is looking at it. | The platform team |
| `DNSReconcileFailed` | The tenant DNS controller on a site failed to reconcile or has not succeeded recently; the platform team is looking at it. | The platform team |
| `Degraded` | IPv6 internet access works with one upstream down; some connections may reset when traffic moves to the other path. | Nobody: it continues by itself |
| `HealthWake` | A repair run was requested for what the health check found. | Nobody: it continues by itself |
| `IPv6EgressUpstreamDown` | IPv6 internet access works through the other node; a node's upstream sessions are down. | The platform team |
| `InfrastructureOverlap` | This network's addresses overlap a range the platform's management connects from; the platform cannot reach it. | You (the tenant) |
| `JobInFlight` | A change is queued; the nodes are checked next time. | Nobody: it continues by itself |
| `LeaseHeld` | A change to the nodes was in progress; they are checked next time. | Nobody: it continues by itself |
| `NoIPv4` | The network has no IPv4 address space; clusters the platform manages need it. | You (the tenant) |
| `NodeHealthRecovered` | Every node is healthy again. | Nobody: it continues by itself |
| `NodeReadFailed` | A node answered but could not be checked this time. | The platform team |
| `NodeRebooted` | A node restarted. | Nobody: it continues by itself |
| `NodeServiceFailed` | A network service on a node is not running. | The platform team |
| `NodeStartFailed` | The network configuration did not start on a node. | The platform team |
| `NotChecked` | The nodes have not been checked yet. | Nobody: it continues by itself |
| `NotEnabled` | Not turned on for this fabric. | Nobody: it continues by itself |
| `NotForManagedClusters` | This address lies in a range the platform's management network routes: no managed cluster's address may. | You (the tenant) |
| `NotOffered` | Not offered on this fabric yet: internet access for an address family, or translation to IPv4 for a network without IPv4. | Nobody: it continues by itself |
| `NotReported` | The site does not report this health check. | Nobody: it continues by itself |
| `NotRequested` | The platform may not reach this network. Allow platform access on the network to build clusters the platform manages. | You (the tenant) |
| `OutsideAddressSpace` | The cluster's address lies outside the network's address space. | You (the tenant) |
| `OutsideAttachableRanges` | Clusters the platform manages need a network inside the ranges your tenant profile lists (managedClusterRanges). | You (the tenant) |
| `PreflightPassed` | Every check before the build passed. | Nobody: it continues by itself |
| `ReadFailed` | A node answered but could not be checked this time. | The platform team |
| `RoutingDaemonRestarted` | A routing process on a node restarted after a crash; the platform team is alerted. | The platform team |
| `ServiceLimitReached` | The platform reaches as many cluster addresses as it may; this cluster waits its turn. Ask the platform team. | The platform team |
| `ServiceNotOffered` | The platform's connection for clusters on networks is not offered here. Ask the platform team. | The platform team |
| `ServiceNotRealized` | The platform's connection for clusters on networks is not realized: one of its settings is wrong. The rest of the fabric works. The platform team acts. | The platform team |
| `ServicePrefixInUse` | An address of this cluster is already reached by the platform for another network. Try again or ask the platform team. | You (the tenant) |
| `ServiceSettingInvalid` | A setting of the platform's connection for clusters on networks is wrong; the nodes keep it as last applied. The platform team acts. | The platform team |
| `ServiceShareReached` | Your clusters hold the share of the platform's cluster addresses one tenant may hold while others use them; this cluster waits its turn. | You (the tenant) |
| `UpstreamDown` | IPv6 internet access is down: no node of the fabric has an upstream session; the platform team is alerted. | The platform team |
<!-- reasons:end -->

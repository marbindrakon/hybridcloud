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
where the platform wrote one.

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

**Failed**

| Reason | What it means | Who acts |
|---|---|---|
| `AccessNotMapped` | No access group named here exists in this tenant; nobody can sign in yet. | You (the tenant) |
| `AsnConflict` | The AS number conflicts with one already in use. | You (the tenant) |
| `AttachmentNotSupported` | This attachment is not supported on the chosen site. | You (the tenant) |
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
| `FabricAmbiguous` | More than one fabric could serve this request. The platform team must resolve it. | The platform team |
| `FabricMismatch` | The network and the site belong to different fabrics. | You (the tenant) |
| `Failed` | Could not be completed; see the reason. | The platform team |
| `FamilyUnavailable` | This address family or IPv6 option is not offered here. The message says whether the site or the network lacks it. | You (the tenant) |
| `IPv6ModeInUse` | The network's IPv6 mode can't change while it has placements. Remove them first, or create a new network. | You (the tenant) |
| `InvalidSecret` | A required credential is not valid. The platform team is looking at it. | The platform team |
| `InvalidSpec` | The request is not valid; correct the fields named in the message. | You (the tenant) |
| `MapperMissing` | Sign-in is not set up for this service yet. The platform team is looking at it. | The platform team |
| `MappingFailed` | The sign-in mapping could not be set up. The platform team is looking at it. | The platform team |
| `NameInUse` | This name is already used by an OpenStack environment of another tenant. Create it with another name. | You (the tenant) |
| `NamespaceNotOwned` | The tenant namespace already exists and was not created for this tenant. The platform team must remove or rename it. | The platform team |
| `NamespaceOperatorImageRejected` | The requested operator version for this tenant is not allowed; the default version is used. | The platform team |
| `NamespaceTaken` | The namespace name is already in use on the site. Choose another name. | You (the tenant) |
| `NoAccount` | No cloud account is set up for this environment. | You (the tenant) |
| `NoDefaultConfig` | No default connection is configured for this service. Ask the platform team. | The platform team |
| `NoProject` | No cloud project is set up for this environment. | You (the tenant) |
| `NotRealizable` | The request cannot be realized on the chosen site. | You (the tenant) |
| `OperatorFailed` | The request could not be queued. The platform team is looking at it. | The platform team |
| `PeerAsnReserved` | This AS number is reserved by the platform. Configure your router with another AS number. | You (the tenant) |
| `PolicyRefused` | The request was refused by a platform policy. | You (the tenant) |
| `PrefixInUse` | The address prefix overlaps one already in use: a placement's subnet or another network of this tenant. | You (the tenant) |
| `PrefixInvalid` | The address prefix is not valid. | You (the tenant) |
| `PrefixReserved` | The address prefix overlaps address space the platform keeps for itself. Choose another prefix. | You (the tenant) |
| `ProbeFailed` | The site check failed. The platform team is looking at it. | The platform team |
| `ProjectDomainNotFound` | The referenced project or domain does not exist. | You (the tenant) |
| `ProvisionFailed` | Creating or updating did not complete. The platform team is looking at it. | The platform team |
| `PublishFailed` | The request could not be queued. The platform team is looking at it. | The platform team |
| `RegionNotSupported` | This region is not supported. | You (the tenant) |
| `RoutesMissing` | Network routes are missing. The platform team is looking at it. | The platform team |
| `SiteNotAllowed` | This site is not available to this tenant. | You (the tenant) |
| `SiteNotEvpnCapable` | The chosen site cannot join hybrid networks. | You (the tenant) |
| `SiteNotVirtualizationCapable` | The chosen site does not offer virtual machines. | You (the tenant) |
| `SiteTypeMismatch` | This kind of environment cannot be created on the chosen site. | You (the tenant) |
| `SiteUnreachable` | The site does not answer. The platform team is looking at it. | The platform team |
| `TeardownFailed` | Deleting did not complete. The platform team is looking at it. | The platform team |
| `TransportNotSupported` | This transport is not supported. | You (the tenant) |
| `TypeNotSupported` | This type is not supported here. | You (the tenant) |
| `ZoneMismatch` | The zone does not match the site. | You (the tenant) |

**On a part of an object (an egress family of a network, a placement's IPv6 internet access)**

| Reason | What it means | Who acts |
|---|---|---|
| `AddressChoice` | IPv6 internet: not offered for tenant-provided prefixes. | You (the tenant) |
| `Degraded` | IPv6 internet access works with one upstream down; some connections may reset when traffic moves to the other path. | Nobody: it continues by itself |
| `NotOffered` | IPv6 internet access is not offered on this fabric yet. | Nobody: it continues by itself |
<!-- reasons:end -->
